# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import math
from typing import Dict, Optional, Tuple

import torch
import torch.nn.functional as F
from fairseq import utils
from fairseq.incremental_decoding_utils import with_incremental_state
from fairseq.modules.fairseq_dropout import FairseqDropout
from fairseq.modules.quant_noise import quant_noise
from torch import Tensor, nn
from torch.nn import Parameter
from fairseq.modules import LayerNorm
import pdb


@with_incremental_state
class GaussianMultiheadAttention(nn.Module):
    """Multi-headed attention.

    See "Attention Is All You Need" for more details.
    """

    def __init__(
        self,
        embed_dim,
        num_heads,
        delta=1,
        kdim=None,
        vdim=None,
        dropout=0.0,
        bias=True,
        add_bias_kv=False,
        add_zero_attn=False,
        self_attention=False,
        encoder_decoder_attention=False,
        q_noise=0.0,
        qn_block_size=8,
        gma_hard=False,
        no_latency_steps=0
    ):
        # print("42: MultiheadAttention init")
        super().__init__()
        self.embed_dim = embed_dim
        self.kdim = kdim if kdim is not None else embed_dim
        self.vdim = vdim if vdim is not None else embed_dim
        self.qkv_same_dim = self.kdim == embed_dim and self.vdim == embed_dim

        self.num_heads = num_heads
        self.dropout_module = FairseqDropout(
            dropout, module_name=self.__class__.__name__
        )

        self.head_dim = embed_dim // num_heads
        assert (
            self.head_dim * num_heads == self.embed_dim
        ), "embed_dim must be divisible by num_heads"
        self.scaling = self.head_dim**-0.5

        self.self_attention = self_attention
        self.encoder_decoder_attention = encoder_decoder_attention

        assert not self.self_attention or self.qkv_same_dim, (
            "Self-attention requires query, key and " "value to be of the same size"
        )

        self.k_proj = quant_noise(
            nn.Linear(self.kdim, embed_dim, bias=bias), q_noise, qn_block_size
        )
        self.v_proj = quant_noise(
            nn.Linear(self.vdim, embed_dim, bias=bias), q_noise, qn_block_size
        )
        self.q_proj = quant_noise(
            nn.Linear(embed_dim, embed_dim, bias=bias), q_noise, qn_block_size
        )
        self.k_hproj = quant_noise(
            nn.Linear(self.kdim, embed_dim, bias=bias), q_noise, qn_block_size
        )
        self.q_hproj = quant_noise(
            nn.Linear(embed_dim, embed_dim, bias=bias), q_noise, qn_block_size
        )

        self.out_proj = quant_noise(
            nn.Linear(embed_dim, embed_dim, bias=bias), q_noise, qn_block_size
        )

        if add_bias_kv:
            self.bias_k = Parameter(torch.Tensor(1, 1, embed_dim))
            self.bias_v = Parameter(torch.Tensor(1, 1, embed_dim))
        else:
            self.bias_k = self.bias_v = None
        
        projection_unit = self.embed_dim
        # self.wp = Parameter(torch.Tensor(self.embed_dim, projection_unit))
        # self.vp = Parameter(torch.Tensor(projection_unit, 1))
        self.wp = nn.Linear(self.embed_dim, projection_unit)
        self.vp = nn.Linear(projection_unit, 1)
        # self.int_attn_layer_norm = LayerNorm(embed_dim)

        # self.vp_head = nn.Linear(projection_unit, num_heads)

        # self.gma_hard=False
        self.delta = delta
        self.max_len = 1100
        self.index_mask = torch.arange(0, self.max_len, device="cuda")
        self.eps = 1e-2
        self.ends=None
        self.add_zero_attn = add_zero_attn

        self.reset_parameters()

        self.onnx_trace = False

    def prepare_for_onnx_export_(self):
        self.onnx_trace = True

    def reset_parameters(self):
        if self.qkv_same_dim:
            # Empirically observed the convergence to be much better with
            # the scaled initialization
            nn.init.xavier_uniform_(self.k_proj.weight, gain=1 / math.sqrt(2))
            nn.init.xavier_uniform_(self.v_proj.weight, gain=1 / math.sqrt(2))
            nn.init.xavier_uniform_(self.q_proj.weight, gain=1 / math.sqrt(2))
            nn.init.xavier_uniform_(self.k_hproj.weight, gain=1 / math.sqrt(2))
            nn.init.xavier_uniform_(self.q_hproj.weight, gain=1 / math.sqrt(2))
        else:
            nn.init.xavier_uniform_(self.k_proj.weight)
            nn.init.xavier_uniform_(self.v_proj.weight)
            nn.init.xavier_uniform_(self.q_proj.weight)
            nn.init.xavier_uniform_(self.k_hproj.weight)
            nn.init.xavier_uniform_(self.q_hproj.weight)
        
        # nn.init.xavier_uniform_(self.vp)
        # nn.init.xavier_uniform_(self.wp)

        nn.init.xavier_uniform_(self.vp.weight)
        # nn.init.xavier_uniform_(self.vp_head.weight)
        nn.init.xavier_uniform_(self.wp.weight)

        nn.init.xavier_uniform_(self.out_proj.weight)
        if self.out_proj.bias is not None:
            nn.init.constant_(self.out_proj.bias, 0.0)
        if self.bias_k is not None:
            nn.init.xavier_normal_(self.bias_k)
        if self.bias_v is not None:
            nn.init.xavier_normal_(self.bias_v)

    def forward(
        self,
        query,
        key: Optional[Tensor],
        value: Optional[Tensor],
        key_padding_mask: Optional[Tensor] = None,
        incremental_state: Optional[Dict[str, Dict[str, Optional[Tensor]]]] = None,
        need_weights: bool = True,
        static_kv: bool = False,
        attn_mask: Optional[Tensor] = None,
        step=None,
        pre_d=None,
        before_softmax: bool = False,
        need_head_weights: bool = False,
        delta=-1,
        gma_hard=False,
        no_latency_steps=0
    ) -> Tuple[Tensor, Optional[Tensor]]:
        """Input shape: Time x Batch x Channel

        Args:
            key_padding_mask (ByteTensor, optional): mask to exclude
                keys that are pads, of shape `(batch, src_len)`, where
                padding elements are indicated by 1s.
            need_weights (bool, optional): return the attention weights,
                averaged over heads (default: False).
            attn_mask (ByteTensor, optional): typically used to
                implement causal attention, where the mask prevents the
                attention from looking forward in time (default: None).
            before_softmax (bool, optional): return the raw attention
                weights and values before the attention softmax.
            need_head_weights (bool, optional): return the attention
                weights for each head. Implies *need_weights*. Default:
                return the average attention weights over all heads.
        """
        if need_head_weights:
            need_weights = True
        # if self.encoder_decoder_attention: pdb.set_trace()
        # self.ends=None
        is_tpu = query.device.type == "xla"

        tgt_len, bsz, embed_dim = query.size()
        src_len = tgt_len
        assert embed_dim == self.embed_dim
        assert list(query.size()) == [tgt_len, bsz, embed_dim]
        #adding from Gaussian 
        
        if key is not None:
            src_len, key_bsz, _ = key.size()
            if not torch.jit.is_scripting():
                assert key_bsz == bsz
                assert value is not None
                assert src_len, bsz == value.shape[:2]

        if (
            not self.onnx_trace
            and not is_tpu  # don't use PyTorch version on TPUs
            and incremental_state is None
            and not static_kv
            # A workaround for quantization to work. Otherwise JIT compilation
            # treats bias in linear module as method.
            and not torch.jit.is_scripting()
        ):
            assert key is not None and value is not None
            return F.multi_head_attention_forward(
                query,
                key,
                value,
                self.embed_dim,
                self.num_heads,
                torch.empty([0]),
                torch.cat((self.q_proj.bias, self.k_proj.bias, self.v_proj.bias)),
                self.bias_k,
                self.bias_v,
                self.add_zero_attn,
                self.dropout_module.p,
                self.out_proj.weight,
                self.out_proj.bias,
                self.training or self.dropout_module.apply_during_inference,
                key_padding_mask,
                need_weights,
                attn_mask,
                use_separate_proj_weight=True,
                q_proj_weight=self.q_proj.weight,
                k_proj_weight=self.k_proj.weight,
                v_proj_weight=self.v_proj.weight,
            )
        if incremental_state is not None:
            saved_state = self._get_input_buffer(incremental_state)
            if saved_state is not None and "prev_key" in saved_state:
                # previous time steps are cached - no need to recompute
                # key and value if they are static
                if static_kv:
                    assert self.encoder_decoder_attention and not self.self_attention
                    key = value = None
        else:
            saved_state = None

        if self.self_attention:
            q = self.q_proj(query)
            k = self.k_proj(query)
            v = self.v_proj(query)
        elif self.encoder_decoder_attention:
            # encoder-decoder attention
            q = self.q_proj(query)
            hq = self.q_hproj(query)
            if key is None:
                assert value is None
                k = v = None
            else:
                k = self.k_proj(key)
                v = self.v_proj(key)
                hk = self.k_hproj(key)

        else:
            assert key is not None and value is not None
            q = self.q_proj(query)
            k = self.k_proj(key)
            v = self.v_proj(value)
        q *= self.scaling

        if self.bias_k is not None:
            assert self.bias_v is not None
            k = torch.cat([k, self.bias_k.repeat(1, bsz, 1)])
            v = torch.cat([v, self.bias_v.repeat(1, bsz, 1)])
            if attn_mask is not None:
                attn_mask = torch.cat(
                    [attn_mask, attn_mask.new_zeros(attn_mask.size(0), 1)], dim=1
                )
            if key_padding_mask is not None:
                key_padding_mask = torch.cat(
                    [
                        key_padding_mask,
                        key_padding_mask.new_zeros(key_padding_mask.size(0), 1),
                    ],
                    dim=1,
                )

        q = (
            q.contiguous()
            .view(tgt_len, bsz * self.num_heads, self.head_dim)
            .transpose(0, 1)
        )
        hq = (
            hq.contiguous()
            .view(tgt_len, bsz * self.num_heads, self.head_dim)
            .transpose(0, 1)
        )
        if k is not None:
            k = (
                k.contiguous()
                .view(-1, bsz * self.num_heads, self.head_dim)
                .transpose(0, 1)
            )
            hk = (
                hk.contiguous()
                .view(-1, bsz * self.num_heads, self.head_dim)
                .transpose(0, 1)
            )
        if v is not None:
            v = (
                v.contiguous()
                .view(-1, bsz * self.num_heads, self.head_dim)
                .transpose(0, 1)
            )

        if saved_state is not None:
            # saved states are stored with shape (bsz, num_heads, seq_len, head_dim)
            if "prev_key" in saved_state:
                _prev_key = saved_state["prev_key"]
                assert _prev_key is not None
                prev_key = _prev_key.view(bsz * self.num_heads, -1, self.head_dim)
                if static_kv:
                    k = prev_key
                else:
                    assert k is not None
                    k = torch.cat([prev_key, k], dim=1)
                src_len = k.size(1)
            if "prev_value" in saved_state:
                _prev_value = saved_state["prev_value"]
                assert _prev_value is not None
                prev_value = _prev_value.view(bsz * self.num_heads, -1, self.head_dim)
                if static_kv:
                    v = prev_value
                else:
                    assert v is not None
                    v = torch.cat([prev_value, v], dim=1)
            prev_key_padding_mask: Optional[Tensor] = None
            if "prev_key_padding_mask" in saved_state:
                prev_key_padding_mask = saved_state["prev_key_padding_mask"]
            assert k is not None and v is not None
            key_padding_mask = GaussianMultiheadAttention._append_prev_key_padding_mask(
                key_padding_mask=key_padding_mask,
                prev_key_padding_mask=prev_key_padding_mask,
                batch_size=bsz,
                src_len=k.size(1),
                static_kv=static_kv,
            )

            saved_state["prev_key"] = k.view(bsz, self.num_heads, -1, self.head_dim)
            saved_state["prev_value"] = v.view(bsz, self.num_heads, -1, self.head_dim)
            saved_state["prev_key_padding_mask"] = key_padding_mask
            # In this branch incremental_state is never None
            assert incremental_state is not None
            incremental_state = self._set_input_buffer(incremental_state, saved_state)
        assert k is not None
        assert k.size(1) == src_len

        # This is part of a workaround to get around fork/join parallelism
        # not supporting Optional types.
        if key_padding_mask is not None and key_padding_mask.dim() == 0:
            key_padding_mask = None

        if key_padding_mask is not None:
            assert key_padding_mask.size(0) == bsz
            assert key_padding_mask.size(1) == src_len

        if self.add_zero_attn:
            assert v is not None
            src_len += 1
            k = torch.cat([k, k.new_zeros((k.size(0), 1) + k.size()[2:])], dim=1)
            v = torch.cat([v, v.new_zeros((v.size(0), 1) + v.size()[2:])], dim=1)
            if attn_mask is not None:
                attn_mask = torch.cat(
                    [attn_mask, attn_mask.new_zeros(attn_mask.size(0), 1)], dim=1
                )
            if key_padding_mask is not None:
                key_padding_mask = torch.cat(
                    [
                        key_padding_mask,
                        torch.zeros(key_padding_mask.size(0), 1).type_as(
                            key_padding_mask
                        ),
                    ],
                    dim=1,
                )

        attn_weights = torch.bmm(q, k.transpose(1, 2))
        attn_weights = self.apply_sparse_mask(attn_weights, tgt_len, src_len, bsz)

        int_attn_weights = torch.bmm(hq, hk.transpose(1, 2))
        int_attn_weights = self.apply_sparse_mask(int_attn_weights, tgt_len, src_len, bsz)

        assert list(attn_weights.size()) == [bsz * self.num_heads, tgt_len, src_len]

        if attn_mask is not None:
            attn_mask = attn_mask.unsqueeze(0)
            if self.onnx_trace:
                attn_mask = attn_mask.repeat(attn_weights.size(0), 1, 1)
            attn_weights += attn_mask
            int_attn_weights += attn_mask

        if key_padding_mask is not None:
            # don't attend to padding symbols
            attn_weights = attn_weights.view(bsz, self.num_heads, tgt_len, src_len)
            if not is_tpu:
                attn_weights = attn_weights.masked_fill(
                    key_padding_mask.unsqueeze(1).unsqueeze(2).to(torch.bool),
                    float("-inf"),
                )
            else:
                attn_weights = attn_weights.transpose(0, 2)
                attn_weights = attn_weights.masked_fill(key_padding_mask, float("-inf"))
                attn_weights = attn_weights.transpose(0, 2)
            attn_weights = attn_weights.view(bsz * self.num_heads, tgt_len, src_len)
        # predict incremental step dp
        # print("===> batch size mha: {} <====".format(bsz))
        # import ipdb; ipdb.set_trace()
        #changes start here 
        # if delta!=None:
        #     import ipdb;ipdb.set_trace()
        #     print("delta is not None: {}".format(delta))
        temp_mask = (
            self.index_mask[:src_len]
            .unsqueeze(0)
            .unsqueeze(1)
            .repeat(bsz * self.num_heads, 1, 1)
            .type_as(attn_weights)
            )
        
        # print("===> temp_mask mha: {} <====".format(temp_mask.shape))

        # temp_mask = (self.index_mask[:src_len].unsqueeze(0).unsqueeze(1).repeat(bsz * self.num_heads, 1, 1).type_as(attn_weights))

        # import ipdb; ipdb.set_trace()
        if self.ends is None or (self.ends.shape[:2]!=attn_weights.shape[:2]):
            # print("====> ends is none and src_len is", src_len)
            self.ends = ( torch.zeros( bsz * self.num_heads, tgt_len, 1 ) + 1).type_as(temp_mask)
        # print("===> ends mha: {} <====".format(self.ends.shape))
        # hist_mask = temp_mask > self.ends
        hist_mask = temp_mask < self.ends


        int_attn=int_attn_weights
        # int_attn=torch.clone(attn_weights)
        int_attn_masked = int_attn.masked_fill(~hist_mask, float("-inf"))
        int_attn = utils.softmax(
            int_attn_masked, dim=-1, onnx_trace=self.onnx_trace
        )
        int_attn = int_attn.type_as(int_attn)
        int_attn = int_attn / int_attn.sum(-1, keepdim=True)
        int_attn_probs = self.dropout_module(int_attn)
        
        # import ipdb; ipdb.set_trace()

        assert v is not None
        int_final_attn = torch.bmm(int_attn_probs.type_as(v), v)
        assert list(int_final_attn.size()) == [bsz * self.num_heads, tgt_len, self.head_dim]

        int_final_attn = int_final_attn.view(bsz, self.num_heads, tgt_len, self.head_dim).transpose(1, 2).contiguous().view(bsz, tgt_len, self.num_heads * self.head_dim)
        int_final_attn = int_final_attn.transpose(0, 1).contiguous().view(tgt_len, bsz, embed_dim)
        int_final_attn = self.out_proj(int_final_attn)
        int_final_attn=int_final_attn.view(bsz, tgt_len, self.num_heads, self.head_dim).transpose(1,2).contiguous().view(bsz*self.num_heads , tgt_len, self.head_dim)
        
        abc=q
        q_new = 0.2*int_final_attn + 0.8*q
        # import ipdb;ipdb.set_trace()
        q_new = q_new / q_new.sum(-1, keepdim=True)

        # import ipdb; ipdb.set_trace()

        # q_new=self.int_attn_layer_norm(q_new)
        h = (
            q_new.contiguous()
            .view(bsz, self.num_heads, tgt_len, self.head_dim)
            .transpose(1, 2)
            .contiguous()
            .view(bsz * tgt_len, self.num_heads * self.head_dim)
        )
        
        # h = (
        #     q.contiguous()
        #     .view(bsz, self.num_heads, tgt_len, self.head_dim)
        #     .transpose(1, 2)
        #     .contiguous()
        #     .view(bsz * tgt_len, self.num_heads * self.head_dim)
        # )
        # dp = torch.exp(torch.mm(torch.tanh(torch.mm(h, self.wp)), self.vp))
        # if self.no_latency_steps<self.num_updates:
        dp = torch.exp(self.vp(torch.tanh(self.wp(h))))
        # dp1 = torch.exp(self.vp_head(torch.tanh(self.wp(h))))
        # dp = dp1.min(dim=-1, keepdim=True)[0]

        dp = (
            dp.contiguous()
            .view(bsz, tgt_len)
            .unsqueeze(1)
            .repeat(1, self.num_heads, 1)
            .contiguous()
            .view(bsz * self.num_heads, tgt_len)
        )

        
        
        dp = torch.cat((torch.zeros(dp.size(0), 1).type_as(dp), dp[:, 1:]), dim=1)
        
        # calculate the aligned source position p

        # import ipdb; ipdb.set_trace()

        if gma_hard:
            p = (
                self.index_mask[:tgt_len]
                .unsqueeze(0)
                .unsqueeze(1)
                .repeat(bsz * self.num_heads, 1, 1)
                .view(bsz * self.num_heads, tgt_len)
                .type_as(dp)
            )

            p += dp #torch.cumsum(dp, dim=1)
        else:
            p = torch.cumsum(dp, dim=1)

        # Avoid p too small during training, for stable training
        p = p.clamp(0.5, float(tgt_len))

        # import ipdb; ipdb.set_trace()

        # calculate the Gaussian distribution (prior)
        p = p.unsqueeze(2)
        varr = p / 2
        index_mask = (
            self.index_mask[:src_len]
            .unsqueeze(0)
            .unsqueeze(1)
            .repeat(bsz * self.num_heads, 1, 1)
            .type_as(p)
        )
        
        # import ipdb; ipdb.set_trace()

        alpha = torch.exp(
            -0.5 * torch.mul((p - index_mask) / varr, (p - index_mask) / varr)
        )
        # mask out the future source
        self.ends = (p + self.delta).ceil()

        if step is not None:
            delta=self.delta
            if self.delta > 1.0:
                self.ends = (p + delta).floor()
            else:
                self.ends = (p + delta).ceil()

        position_in_attended_cut = index_mask

        future_mask = position_in_attended_cut < self.ends
        # import ipdb; ipdb.set_trace()
        alpha = torch.mul(alpha, future_mask.float())
        attn_weights_masked = attn_weights.masked_fill(~future_mask, float("-inf"))
        
        attn_weights = utils.softmax(
        attn_weights_masked, dim=-1, onnx_trace=self.onnx_trace)
        attn_weights = attn_weights.type_as(attn_weights)
        attn_weights = torch.mul(attn_weights, alpha)

        import ipdb; ipdb.set_trace()


        # else:
        #     # print("================> delta is None")
        #     attn_weights = utils.softmax(
        #         attn_weights, dim=-1, onnx_trace=self.onnx_trace)
        #     attn_weights = attn_weights.type_as(attn_weights)
        # # if before_softmax:
        #     return attn_weights, v
        # import ipdb; ipdb.set_trace()

        
        # calculate the final attention (posterior)

        
        attn_weights = attn_weights / attn_weights.sum(-1, keepdim=True)
        attn_probs = self.dropout_module(attn_weights)

        assert v is not None
        attn = torch.bmm(attn_probs.type_as(v), v)
        assert list(attn.size()) == [bsz * self.num_heads, tgt_len, self.head_dim]
        if self.onnx_trace and attn.size(1) == 1:
            # when ONNX tracing a single decoder step (sequence length == 1)
            # the transpose is a no-op copy before view, thus unnecessary
            attn = attn.contiguous().view(tgt_len, bsz, embed_dim)
        else:
            attn = attn.transpose(0, 1).contiguous().view(tgt_len, bsz, embed_dim)
        attn = self.out_proj(attn) # [tgt_len, bsz, h_dim]

        # attn_weights: Optional[Tensor] = None

        if need_weights:
            attn_weights = attn_weights.view(
                bsz, self.num_heads, tgt_len, src_len
            ).transpose(1, 0)
            # import ipdb; ipdb.set_trace()
            # if not need_head_weights:
                # average attention weights over heads
                # attn_weights = attn_weights.mean(dim=0)
        # import ipdb; ipdb.set_trace()
        if step is not None:
            return attn, attn_weights, self.ends
        else:
            return attn, attn_weights, None

        # return attn, attn_weights

    @staticmethod
    def _append_prev_key_padding_mask(
        key_padding_mask: Optional[Tensor],
        prev_key_padding_mask: Optional[Tensor],
        batch_size: int,
        src_len: int,
        static_kv: bool,
    ) -> Optional[Tensor]:
        # saved key padding masks have shape (bsz, seq_len)
        if prev_key_padding_mask is not None and static_kv:
            new_key_padding_mask = prev_key_padding_mask
        elif prev_key_padding_mask is not None and key_padding_mask is not None:
            new_key_padding_mask = torch.cat(
                [prev_key_padding_mask.float(), key_padding_mask.float()], dim=1
            )
        # During incremental decoding, as the padding token enters and
        # leaves the frame, there will be a time when prev or current
        # is None
        elif prev_key_padding_mask is not None:
            if src_len > prev_key_padding_mask.size(1):
                filler = torch.zeros(
                    (batch_size, src_len - prev_key_padding_mask.size(1)),
                    device=prev_key_padding_mask.device,
                )
                new_key_padding_mask = torch.cat(
                    [prev_key_padding_mask.float(), filler.float()], dim=1
                )
            else:
                new_key_padding_mask = prev_key_padding_mask.float()
        elif key_padding_mask is not None:
            if src_len > key_padding_mask.size(1):
                filler = torch.zeros(
                    (batch_size, src_len - key_padding_mask.size(1)),
                    device=key_padding_mask.device,
                )
                new_key_padding_mask = torch.cat(
                    [filler.float(), key_padding_mask.float()], dim=1
                )
            else:
                new_key_padding_mask = key_padding_mask.float()
        else:
            new_key_padding_mask = prev_key_padding_mask
        return new_key_padding_mask

    @torch.jit.export
    def reorder_incremental_state(
        self,
        incremental_state: Dict[str, Dict[str, Optional[Tensor]]],
        new_order: Tensor,
    ):
        """Reorder buffered internal state (for incremental generation)."""
        input_buffer = self._get_input_buffer(incremental_state)
        if input_buffer is not None:
            for k in input_buffer.keys():
                input_buffer_k = input_buffer[k]
                if input_buffer_k is not None:
                    if self.encoder_decoder_attention and input_buffer_k.size(
                        0
                    ) == new_order.size(0):
                        break
                    input_buffer[k] = input_buffer_k.index_select(0, new_order)
            incremental_state = self._set_input_buffer(incremental_state, input_buffer)
        return incremental_state

    def _get_input_buffer(
        self, incremental_state: Optional[Dict[str, Dict[str, Optional[Tensor]]]]
    ) -> Dict[str, Optional[Tensor]]:
        result = self.get_incremental_state(incremental_state, "attn_state")
        if result is not None:
            return result
        else:
            empty_result: Dict[str, Optional[Tensor]] = {}
            return empty_result

    def _set_input_buffer(
        self,
        incremental_state: Dict[str, Dict[str, Optional[Tensor]]],
        buffer: Dict[str, Optional[Tensor]],
    ):
        return self.set_incremental_state(incremental_state, "attn_state", buffer)

    def apply_sparse_mask(self, attn_weights, tgt_len: int, src_len: int, bsz: int):
        return attn_weights

    def upgrade_state_dict_named(self, state_dict, name):
        prefix = name + "." if name != "" else ""
        items_to_add = {}
        keys_to_remove = []
        for k in state_dict.keys():
            if k.endswith(prefix + "in_proj_weight"):
                # in_proj_weight used to be q + k + v with same dimensions
                dim = int(state_dict[k].shape[0] / 3)
                items_to_add[prefix + "q_proj.weight"] = state_dict[k][:dim]
                items_to_add[prefix + "k_proj.weight"] = state_dict[k][dim : 2 * dim]
                items_to_add[prefix + "v_proj.weight"] = state_dict[k][2 * dim :]

                keys_to_remove.append(k)

                k_bias = prefix + "in_proj_bias"
                if k_bias in state_dict.keys():
                    dim = int(state_dict[k].shape[0] / 3)
                    items_to_add[prefix + "q_proj.bias"] = state_dict[k_bias][:dim]
                    items_to_add[prefix + "k_proj.bias"] = state_dict[k_bias][
                        dim : 2 * dim
                    ]
                    items_to_add[prefix + "v_proj.bias"] = state_dict[k_bias][2 * dim :]

                    keys_to_remove.append(prefix + "in_proj_bias")

        for k in keys_to_remove:
            del state_dict[k]

        for key, value in items_to_add.items():
            state_dict[key] = value