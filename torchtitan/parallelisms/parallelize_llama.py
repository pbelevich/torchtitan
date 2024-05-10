# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# this file applies the PTD parallelisms and various training techniques to the
# llama model, i.e. activation checkpointing, etc.

from collections import defaultdict
from typing import Dict, List, Tuple

import torch

# TODO(whc) this can be removed after pippy migration into pytorch core is complete.
try:
    from pippy import (
        ManualPipelineStage,
        pipeline,
        Schedule1F1B,
        ScheduleGPipe,
        SplitPoint,
    )
    from pippy.PipelineStage import _PipelineStage
except ImportError as exc:
    raise ImportError(
        "pippy is not installed. Please install it to use pipeline parallelism. "
        "`pip install git+https://github.com/pytorch/pippy`"
    ) from exc

from torch.distributed._composable.fsdp import fully_shard, MixedPrecisionPolicy
from torch.distributed._tensor import Replicate, Shard
from torch.distributed.algorithms._checkpoint.checkpoint_wrapper import (
    checkpoint_wrapper as ptd_checkpoint_wrapper,
    CheckpointImpl,
)
from torch.distributed.tensor.parallel import (
    ColwiseParallel,
    parallelize_module,
    PrepareModuleInput,
    RowwiseParallel,
    SequenceParallel,
)
from torch.nn import ModuleDict
from torch.utils.checkpoint import _pt2_selective_checkpoint_context_fn_gen, checkpoint

from torchtitan.config_manager import JobConfig
from torchtitan.logging_utils import logger


# for selective AC
no_recompute_list = {
    torch.ops.aten.mm.default,
    torch.ops.aten._scaled_dot_product_efficient_attention.default,
    torch.ops.aten._scaled_dot_product_flash_attention.default,
    torch.ops._c10d_functional.reduce_scatter_tensor.default,
}


# Uses PTD FSDP AC wrapper
# currently selective per op and per layer checkpointing are supported
def checkpoint_wrapper(module, config):
    if config.mode == "selective" and config.selective_ac_option == "op":

        def _get_custom_policy(meta):
            def _custom_policy(mode, func, *args, **kwargs):
                mm_count_key = f"{mode}_mm_count"
                if func == torch.ops.aten.mm.default:
                    meta[mm_count_key] += 1
                # Saves output of all compute ops, except every second mm
                return func in no_recompute_list and not (
                    func == torch.ops.aten.mm.default and meta[mm_count_key] % 2 == 0
                )

            return _custom_policy

        def selective_checkpointing_context_fn():
            meta = defaultdict(int)
            return _pt2_selective_checkpoint_context_fn_gen(_get_custom_policy(meta))

        return ptd_checkpoint_wrapper(
            module,
            checkpoint_impl=CheckpointImpl.NO_REENTRANT,
            checkpoint_fn=checkpoint,
            context_fn=selective_checkpointing_context_fn,
            use_reentrant=False,
            preserve_rng_state=False,
        )
    elif config.mode == "full":
        return ptd_checkpoint_wrapper(
            module,
            checkpoint_impl=CheckpointImpl.NO_REENTRANT,
            checkpoint_fn=checkpoint,
            use_reentrant=False,
            preserve_rng_state=False,
        )

    elif config.mode == "selective" and config.selective_ac_option.isdigit():
        """enables selective checkpointing of candidate layers.
        Usage:
        'selective_ac_option' with a positive 'int' value in config controls which layers to checkpoint.
        1 == checkpointing every one (all).
        2 == checkpoint every 2nd one
        """
        every_x_layer = int(config.selective_ac_option)
        assert (
            every_x_layer >= 0
        ), f"selective layer AC policy (every_x_layer) expects a positive integer, received {every_x_layer}"

        checkpoint_wrapper.__dict__.setdefault("_count", 0)

        checkpoint_wrapper._count += 1
        if not every_x_layer or checkpoint_wrapper._count % every_x_layer == 0:
            return ptd_checkpoint_wrapper(
                module,
                checkpoint_impl=CheckpointImpl.NO_REENTRANT,
                checkpoint_fn=checkpoint,
                use_reentrant=False,
                preserve_rng_state=False,
            )
        # skip activation checkpointing and store activations for this layer
        else:
            return module

    else:
        raise NotImplementedError(
            "Unknown AC type or AC config. Only selective op and selective layer ac implemented currently."
        )


def get_tp_parallel_strategy(
    job_config: JobConfig,
) -> Tuple[RowwiseParallel, ColwiseParallel]:
    """Get the parallel strategy for the transformer model.

    This function handles the special case of using float8 with tensor parallelism.
    """
    if job_config.training.fp8_linear == "dynamic":
        from float8_experimental.float8_tensor_parallel import (
            Float8ColwiseParallel,
            Float8RowwiseParallel,
        )

        return Float8RowwiseParallel, Float8ColwiseParallel
    return RowwiseParallel, ColwiseParallel


class TransformerChunk(torch.nn.Module):
    def __init__(
        self,
        orig_model,  # : Transformer,
        this_stage_layer_names: List[str],
        device,
        input_seqlen: int,
    ):
        super().__init__()
        self.tok_embeddings = None

        # inferring seqlen from forward(input) only works on stage0, bc on later stages
        # the hidden state input may have reduced seqlen due to TP.  We need to use the
        # original (full) seqlen for freqs_cis to be correct.
        self.input_seqlen = input_seqlen

        if "tok_embeddings" in this_stage_layer_names:
            self.tok_embeddings = orig_model.tok_embeddings

        with torch.device(device):
            self.freqs_cis = orig_model._precompute_freqs_cis()

        # preserve FQNs of original model by preserving structure
        # (including preserving position in layers[] list)- use dummy module
        self.layers = ModuleDict()
        for name in this_stage_layer_names:
            if "layers." in name:
                idx = name.split(".")[-1]
                self.layers[idx] = orig_model.layers[int(idx)]
        self.norm = None
        if "norm" in this_stage_layer_names:
            self.norm = orig_model.norm
        self.output = None
        if "output" in this_stage_layer_names:
            self.output = orig_model.output

    def forward(self, input):
        """
        Copypaste of original Transformer.forward, with conditionals and unpacking added
        such that we handle the cases where this rank doesn't have the embedding, or doesn't have
        the output layers.
        """
        if self.tok_embeddings:
            h = self.tok_embeddings(input)
        else:
            h = input

        freqs_cis = self.freqs_cis[0 : self.input_seqlen]

        for layer in self.layers.values():
            h = layer(h, freqs_cis)
        output = h

        if self.norm:
            h = self.norm(h)
            output = h

        if self.output:
            output = self.output(h).float()
        return output


def apply_pipeline_parallelism(
    model, world_mesh, parallel_dims, job_config: JobConfig, device, model_config: Dict
):
    if job_config.experimental.pipeline_parallel_split_mode == "manual":
        return apply_pipeline_parallelism_manual(
            model, world_mesh, parallel_dims, job_config, device, model_config
        )
    elif job_config.experimental.pipeline_parallel_split_mode == "tracer":
        return apply_pipeline_parallelism_tracer(
            model, world_mesh, parallel_dims, job_config, device, model_config
        )
    else:
        raise NotImplementedError(
            f"{job_config.experimental.pipeline_parallel_split_mode} is not a valid split mode"
        )


def build_pipeline_schedule(job_config, parallel_dims, stage, loss_fn):
    if job_config.experimental.pipeline_parallel_schedule == "1f1b":
        schedule_class = Schedule1F1B
    elif job_config.experimental.pipeline_parallel_schedule == "gpipe":
        schedule_class = ScheduleGPipe
    else:
        raise NotImplementedError(
            f"{job_config.experimental.pipeline_parallel_schedule} is not implemented"
        )
    return schedule_class(
        stage,
        n_microbatches=parallel_dims.pp,
        loss_fn=loss_fn,
    )


def apply_pipeline_parallelism_manual(
    model, world_mesh, parallel_dims, job_config: JobConfig, device, model_config: Dict
):
    """
    This API gets individual torch.nn.Module objects for each pipeline stage (including virtual stages).

    The SPMD parallelisms should be applied to
    """
    pp_mesh = world_mesh["pp"]
    pp_rank = pp_mesh.get_local_rank()
    pp_size = pp_mesh.size()
    # heuristically == PP dim but should be a config
    microbatches = parallel_dims.pp
    stage_idx = pp_rank  # TODO support virtual stages
    layers_per_rank = len(model.layers) // parallel_dims.pp
    layer_offset = layers_per_rank * pp_rank
    this_stage_layer_names = [
        f"layers.{i + layer_offset}" for i in range(layers_per_rank)
    ]
    if pp_rank == 0:
        this_stage_layer_names.insert(0, "tok_embeddings")
        assert "layers.0" in this_stage_layer_names
    elif pp_rank == pp_size - 1:
        this_stage_layer_names.append("norm")
        this_stage_layer_names.append("output")
        assert "layers.1" in this_stage_layer_names

    input_seqlen = 2048  # TODO hack

    model = TransformerChunk(model, this_stage_layer_names, device, input_seqlen)
    # Create a pipeline representation from the model

    # TODO(whc) once ManualPipelineStage supports lazy shape inference, we can leave model on meta device longer and
    # get rid of the input shape hardcoded here. For now, it should not be a big deal since we only materialize the
    # layers of the model that map to this stage, not the whole model.

    # Get example input
    if pp_rank == 0:
        input_shape = (job_config.training.batch_size, job_config.training.seq_len)
        input = torch.randint(
            model_config.vocab_size, input_shape, dtype=torch.int64, device=device
        )

        # HACK- can't use shape inference via execution of the PP stage inside ManualPipelineStage API, becuase the
        # real output shapes will change after applying TP.  So we hardcode output shapes here, and thus bypass doing
        # shape inference.
        # the real fix is to use lazy shape inference during first PP forward, and not need to specify anything here.
        output_shape = (
            job_config.training.batch_size,
            int(job_config.training.seq_len // parallel_dims.tp),
            model_config.dim,
        )
        output = torch.empty(output_shape, dtype=torch.float32, device=device)
    else:
        # TODO(whc) can we rely on shape inference so that user doesn't have to compute TP impact on seq_len
        input_shape = (
            job_config.training.batch_size,
            int(job_config.training.seq_len // parallel_dims.tp),
            model_config.dim,
        )
        input = torch.randint(
            model_config.vocab_size, input_shape, dtype=torch.float32, device=device
        )
        # TODO wrong shape, need to consider output layer
        output_shape = (
            job_config.training.batch_size,
            int(job_config.training.seq_len // parallel_dims.tp),
            model_config.dim,
        )
        output = torch.empty(output_shape, dtype=torch.float32, device=device)

    model.to_empty(device=device)
    stage = ManualPipelineStage(
        model,
        pp_rank,
        pp_size,
        device,
        microbatches,
        input_args=input.chunk(microbatches)[0],
        output_args=output.chunk(microbatches)[0],
        group=pp_mesh.get_group("pp"),
    )
    return (stage, model)


def apply_pipeline_parallelism_tracer(
    model, world_mesh, parallel_dims, job_config: JobConfig, device, model_config: Dict
):
    assert (
        parallel_dims.pp_enabled
    ), "can't apply pipeline parallelism if it is not enabled"

    if job_config.model.norm_type == "fused_rmsnorm":
        # TODO(whc) - torch._dynamo.exc.Unsupported: Illegal getattr invocation stride in strict mode
        # coming from ` if dy.stride(-1) != 1:` in fused_rmsnorm
        raise NotImplementedError(
            "fused_rmsnorm not yet compatible with Pipeline Tracer (strides error). Please use layernorm or rmsnorm."
        )
    pp_mesh = world_mesh["pp"]
    pp_rank = pp_mesh.get_local_rank()
    stage_idx = pp_mesh.get_local_rank()
    layers_per_rank = len(model.layers) // parallel_dims.pp
    split_spec = {
        f"layers.{i * layers_per_rank}": SplitPoint.BEGINNING
        for i in range(1, parallel_dims.pp)
    }
    # Get example input
    input_shape = (job_config.training.batch_size, job_config.training.seq_len)
    input_ids = torch.randint(
        model.vocab_size, input_shape, dtype=torch.int64, device="meta"
    )

    # Create a pipeline representation from the model
    pipe = pipeline(
        model, parallel_dims.pp, example_args=(input_ids,), split_spec=split_spec
    )
    model = pipe.get_stage_module(stage_idx)
    stage = _PipelineStage(
        stage_module=model,
        stage_index=pp_rank,
        pipe_info=pipe.pipe_info,
        device=device,
        group=pp_mesh.get_group(),
    )
    return (stage, model)


def parallelize_llama(model, world_mesh, parallel_dims, job_config: JobConfig):
    """
    Apply SPMD parallelisms and activation checkpointing to the model.

    NOTE: The passed-in model preferably should be on meta device. Otherwise,
    the model must fit on GPU or CPU memory.
    """

    if parallel_dims.tp_enabled:
        if job_config.model.norm_type == "fused_rmsnorm":
            raise NotImplementedError(
                "fused_rmsnorm not yet compatible with TP. Please use layernorm or rmsnorm."
            )

        tp_mesh = world_mesh["tp"]
        row_parallel_strategy, col_parallel_strategy = get_tp_parallel_strategy(
            job_config
        )
        loss_parallel = parallel_dims.loss_parallel_enabled

        # 1. Parallelize the first embedding and the last linear proj layer
        # 2. Parallelize the root norm layer over the sequence dim
        # 3. Shard the first transformer block's inputs
        model = parallelize_module(
            model,
            tp_mesh,
            {
                "tok_embeddings": RowwiseParallel(
                    input_layouts=Replicate(),
                    output_layouts=Shard(1),
                ),
                "output": col_parallel_strategy(
                    input_layouts=Shard(1),
                    output_layouts=(Shard(-1) if loss_parallel else Replicate()),
                    use_local_output=not loss_parallel,
                ),
                "norm": SequenceParallel(),
            },
        )

        # Apply tensor + sequence parallelism to every transformer block
        for layer_name, transformer_block in model.layers.named_children():
            layer_plan = {
                "attention": PrepareModuleInput(
                    input_layouts=(Shard(1), None),
                    desired_input_layouts=(Replicate(), None),
                ),
                "attention.wq": col_parallel_strategy(),
                "attention.wk": col_parallel_strategy(),
                "attention.wv": col_parallel_strategy(),
                "attention.wo": row_parallel_strategy(output_layouts=Shard(1)),
                "attention_norm": SequenceParallel(),
                "feed_forward": PrepareModuleInput(
                    input_layouts=(Shard(1),),
                    desired_input_layouts=(Replicate(),),
                ),
                "feed_forward.w1": col_parallel_strategy(),
                "feed_forward.w2": row_parallel_strategy(output_layouts=Shard(1)),
                "feed_forward.w3": col_parallel_strategy(),
                "ffn_norm": SequenceParallel(),
            }

            # Adjust attention module to use the local number of heads
            attn_layer = transformer_block.attention
            attn_layer.n_heads = attn_layer.n_heads // tp_mesh.size()
            attn_layer.n_kv_heads = attn_layer.n_kv_heads // tp_mesh.size()

            parallelize_module(
                module=transformer_block,
                device_mesh=tp_mesh,
                parallelize_plan=layer_plan,
            )

        logger.info("Applied Tensor Parallelism to the model")

    if parallel_dims.dp_enabled:
        dp_mesh = world_mesh["dp"] if world_mesh.ndim > 1 else world_mesh
        assert dp_mesh.mesh_dim_names == ("dp",), dp_mesh.mesh_dim_names
        # TODO: Expose `reduce_dtype` as a config option.
        mp_policy = MixedPrecisionPolicy(
            # TODO(whc) need to fix PP + FSDP-mixed-precision
            # tracer for PP assumes f32 and is caught off guard when runtime FSDP interacts using bf16 inputs
            # param_dtype=torch.bfloat16, reduce_dtype=torch.float32
            param_dtype=torch.float32,
            reduce_dtype=torch.float32,
        )
        ac_mode = job_config.activation_checkpoint.mode
        fsdp_config = {"mesh": dp_mesh, "mp_policy": mp_policy}
        for layer_name, transformer_block in model.layers.named_children():
            if job_config.activation_checkpoint.mode in ("full", "selective"):
                transformer_block = checkpoint_wrapper(
                    transformer_block, job_config.activation_checkpoint
                )
            # As an optimization, do not reshard after forward for the last
            # transformer block since FSDP would prefetch it immediately
            # reshard_after_forward = layer_id < len(model.layers) - 1
            # TODO(whc) need to fix correctly handle layer-ids on pp-split module
            reshard_after_forward = True
            fully_shard(
                transformer_block,
                **fsdp_config,
                reshard_after_forward=reshard_after_forward,
            )
            model.layers.add_module(layer_name, transformer_block)

        # TODO(whc) do we need reshard_after_forward setting here too?
        model = fully_shard(model, **fsdp_config)
        if ac_mode in ("full", "selective"):
            logger.info(f"Applied {ac_mode} activation checkpointing to the model")
        logger.info("Applied FSDP to the model")

    return model
