from typing import Optional

import torch
from torch import nn, Tensor
import torch.nn.functional as F
from torch.nn import Module, ModuleList

import einx
from einx import rearrange

from ring_attention_pytorch.ring import (
    all_ring_pass,
    is_distributed,
    get_rank,
    get_world_size
)

from ring_attention_pytorch.ring_flash_attention import (
    ring_flash_attn
)

from ring_attention_pytorch.distributed import (
    split_by_rank,
    AllGather
)

# helper functions

def exists(v):
    return v is not None

def default(v, d):
    return v if exists(v) else d

def divisible_by(num, den):
    return (num % den) == 0

def default_attention(
    q: Tensor,
    k: Tensor,
    v: Tensor,
    mask: Optional[Tensor],
    causal: bool = False
):
    mask_value = -torch.finfo(q.dtype).max

    # similarity

    sim = einx.dot('b h i d, b h j d -> b h i j', q, k)

    # masking

    if causal:
        i, j = sim.shape[-2:]
        causal_mask = torch.ones((i, j), dtype = torch.bool).triu(j - i + 1)
        sim = einx.where('i j, , b h i j -> b h i j', causal_mask, mask_value, sim)

    elif exists(mask):
        sim = einx.where('b j, b h i j, -> b h i j', mask, sim, mask_value)

    # attend

    attn = einx.softmax('b h i [j]', sim)

    # aggregate

    out = einx.dot('b h i j, b h j d -> b h i d', attn, v)

    return out

# batch to sequence sharding and back

def pad_to_multiple(
    x: Tensor,
    length: int,
    pad_value = 0
):
    seq_len = x.shape[-1]
    remainder = seq_len % length

    if remainder == 0:
        return x, 0

    pad_length = length - remainder
    return F.pad(x, (0, pad_length), value = pad_value), pad_length

def sharded_batch_to_sharded_seq(
    x: Tensor,
    mask: Optional[Tensor],
    seq_size: int
):
    assert is_distributed()

    orig_x, seq_len = x, x.shape[-1]

    # auto pad sequence and mask, as ring passing makes assumption tensor is all same shape

    x, pad_length = pad_to_multiple(x, seq_size)

    if pad_length > 0:
        if not exists(mask):
            mask = torch.ones_like(orig_x).bool()

        mask = pad_to_multiple(mask, seq_size, pad_value = False)

    # all gather across batch

    all_gather = AllGather(dim = 0)

    x, sizes = all_gather(x)

    if exists(mask):
        mask = all_gather(mask)

    # then split sequence across machines

    x = x.split(seq_size, dim = -1)

    assert len(x) == get_world_size()

    x, _ = split_by_rank(x)

    if exists(mask):
        mask = mask.split(seq_size, dim = -1)
        mask = split_by_rank(mask)

    return (x, mask), sizes

def sharded_seq_to_sharded_batch(
    logits: Tensor,
    sizes
):
    all_gather = AllGather(dim = -2) # all gather across sequence

    logits, _ = all_gather(logits)

    logits = logits.split(sizes.tolist(), dim = 0)

    logits = split_by_rank(logits)

    return logits

# main class

class RingAttention(Module):
    def __init__(
        self,
        dim,
        *,
        dim_head = 64,
        heads = 8,
        causal = False,
        eps = 1e-10,
        q_bucket_size = 512,
        k_bucket_size = 512,
        ring_attn = False,
        ring_seq_size = 512,
        auto_shard_seq = None,
        prenorm = True
    ):
        super().__init__()
        self.eps = eps
        self.heads = heads
        self.scale = dim_head ** -0.5
        self.prenorm = prenorm
        self.causal = causal

        assert divisible_by(ring_seq_size, q_bucket_size)
        assert divisible_by(ring_seq_size, k_bucket_size)

        self.ring_attn = ring_attn
        self.auto_shard_seq = default(auto_shard_seq, ring_attn) # this should be done at the transformer level on the token ids for efficiency, but for testing purposes

        assert not (not self.ring_attn and self.auto_shard_seq)

        self.ring_seq_size = ring_seq_size

        self.q_bucket_size = q_bucket_size
        self.k_bucket_size = k_bucket_size

        dim_inner = dim_head * heads
        self.to_qkv = nn.Sequential(
            RMSNorm(dim),
            nn.Linear(dim, dim_inner * 3, bias = False)
        )

        self.to_out = nn.Linear(dim_inner, dim, bias = False)

    def forward(
        self,
        x,
        mask = None
    ):
        """
        einstein notation

        b - batch
        h - heads
        d - feature dimension
        n, i, j - sequence
        """

        ring_attn = self.ring_attn & is_distributed()
        auto_shard_seq = self.auto_shard_seq & is_distributed()

        seq_len = x.shape[-1]

        if auto_shard_seq:
            (x, mask), batch_sizes = sharded_batch_to_sharded_seq(x, mask, self.ring_seq_size)

        device = x.device

        qkv = self.to_qkv(x)
        q, k, v = rearrange('b n (qkv h d) -> qkv b h n d', qkv, qkv = 3, h = self.heads)

        q = q * self.scale

        if not is_distributed():
            out = default_attention(q, k, v, mask = mask, causal = self.causal)
        else:
            out = ring_flash_attn(
                q, k, v,
                mask,
                self.causal,
                self.q_bucket_size,
                self.k_bucket_size,
                ring_attn
            )

        # combine heads

        out = rearrange('b h n d -> b n (h d)', out)
        out = self.to_out(out)

        if auto_shard_seq:
            out, _ = sharded_seq_to_sharded_batch(out, batch_sizes)
            out = out[:, :seq_len]

        return out
# simple transformer for end2end testing

class RMSNorm(Module):
    def __init__(self, dim):
        super().__init__()
        self.scale = dim ** 0.5
        self.gamma = nn.Parameter(torch.ones(dim))

    def forward(self, x):
        return F.normalize(x, dim = -1) * self.scale * self.gamma

def FeedForward(dim, mult = 4):
    dim_inner = int(dim * mult)
    return nn.Sequential(
        RMSNorm(dim),
        nn.Linear(dim, dim_inner),
        nn.GELU(),
        nn.Linear(dim_inner, dim)
    )

class RingTransformer(Module):
    def __init__(
        self,
        *,
        num_tokens,
        dim,
        depth,
        causal = False,
        dim_head = 64,
        heads = 8,
        ff_mult = 4,
        q_bucket_size = 512,
        k_bucket_size = 512,
        ring_attn = False,
        ring_seq_size = 512,
        auto_shard_seq = None,
    ):
        super().__init__()
        self.ring_attn = ring_attn
        self.ring_seq_size = ring_seq_size
        self.auto_shard_seq = default(auto_shard_seq, ring_attn) # if ring attention is turned on, auto-shard across sequence dimension. this can also be turned off and done manually elsewhere in the data loading

        assert not (not self.ring_attn and self.auto_shard_seq)

        self.token_emb = nn.Embedding(num_tokens, dim)

        self.layers = ModuleList([])

        for _ in range(depth):
            self.layers.append(ModuleList([
                RingAttention(
                    dim = dim,
                    causal = causal,
                    dim_head = dim_head,
                    heads = heads,
                    q_bucket_size = q_bucket_size,
                    k_bucket_size = k_bucket_size,
                    ring_attn = ring_attn,
                    ring_seq_size = ring_seq_size,
                    auto_shard_seq = False,
                ),
                FeedForward(dim = dim, mult = ff_mult)
            ]))

        self.to_logits = nn.Sequential(
            RMSNorm(dim),
            nn.Linear(dim, num_tokens, bias = False)
        )

    def forward(
        self,
        x,
        mask = None
    ):
        seq_len = x.shape[-1]
        auto_shard_seq = self.auto_shard_seq & is_distributed()

        if auto_shard_seq:
            (x, mask), batch_sizes = sharded_batch_to_sharded_seq(x, mask, self.ring_seq_size)

        x = self.token_emb(x)

        for attn, ff in self.layers:
            x = attn(x, mask = mask) + x
            x = ff(x) + x

        logits = self.to_logits(x)

        if auto_shard_seq:
            logits, _ = sharded_seq_to_sharded_batch(logits, batch_sizes)
            logits = logits[:, :seq_len]

        return logits
