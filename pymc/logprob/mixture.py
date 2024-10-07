#   Copyright 2024 The PyMC Developers
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
#   MIT License
#
#   Copyright (c) 2021-2022 aesara-devs
#
#   Permission is hereby granted, free of charge, to any person obtaining a copy
#   of this software and associated documentation files (the "Software"), to deal
#   in the Software without restriction, including without limitation the rights
#   to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
#   copies of the Software, and to permit persons to whom the Software is
#   furnished to do so, subject to the following conditions:
#
#   The above copyright notice and this permission notice shall be included in all
#   copies or substantial portions of the Software.
#
#   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#   IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#   FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#   AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#   LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
#   OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
#   SOFTWARE.

from typing import cast

import pytensor
import pytensor.tensor as pt

from pytensor.graph.basic import Apply, Constant, Variable, ancestors
from pytensor.graph.fg import FunctionGraph
from pytensor.graph.op import Op, compute_test_value
from pytensor.graph.rewriting.basic import EquilibriumGraphRewriter, node_rewriter
from pytensor.ifelse import IfElse, ifelse
from pytensor.scalar import Switch
from pytensor.scalar import switch as scalar_switch
from pytensor.tensor.basic import Join, MakeVector, switch
from pytensor.tensor.random.rewriting import (
    local_dimshuffle_rv_lift,
    local_rv_size_lift,
    local_subtensor_rv_lift,
)
from pytensor.tensor.rewriting.shape import ShapeFeature
from pytensor.tensor.shape import shape_tuple
from pytensor.tensor.subtensor import (
    AdvancedSubtensor,
    AdvancedSubtensor1,
    as_index_literal,
    get_canonical_form_slice,
    is_basic_idx,
)
from pytensor.tensor.type import TensorType
from pytensor.tensor.type_other import NoneConst, NoneTypeT, SliceConstant, SliceType
from pytensor.tensor.variable import TensorVariable

from pymc.logprob.abstract import (
    MeasurableElemwise,
    MeasurableOp,
    PromisedValuedRV,
    _logprob,
    _logprob_helper,
    valued_rv,
)
from pymc.logprob.rewriting import (
    early_measurable_ir_rewrites_db,
    local_lift_DiracDelta,
    measurable_ir_rewrites_db,
    subtensor_ops,
)
from pymc.logprob.utils import (
    check_potential_measurability,
    filter_measurable_variables,
    get_related_valued_nodes,
)
from pymc.pytensorf import constant_fold


def is_newaxis(x):
    return isinstance(x, type(None)) or isinstance(getattr(x, "type", None), NoneTypeT)


def expand_indices(
    indices: tuple[Variable | slice | None, ...], shape: tuple[TensorVariable]
) -> tuple[TensorVariable]:
    """Convert basic and/or advanced indices into a single, broadcasted advanced indexing operation.

    Parameters
    ----------
    indices
        The indices to convert.
    shape
        The shape of the array being indexed.

    """
    n_non_newaxis = sum(1 for idx in indices if not is_newaxis(idx))
    n_missing_dims = len(shape) - n_non_newaxis
    full_indices = list(indices) + [slice(None)] * n_missing_dims

    # We need to know if a "subspace" was generated by advanced indices
    # bookending basic indices.  If so, we move the advanced indexing subspace
    # to the "front" of the shape (i.e. left-most indices/last-most
    # dimensions).
    index_types = [is_basic_idx(idx) for idx in full_indices]

    first_adv_idx = len(shape)
    try:
        first_adv_idx = index_types.index(False)
        first_bsc_after_adv_idx = index_types.index(True, first_adv_idx)
        index_types.index(False, first_bsc_after_adv_idx)
        moved_subspace = True
    except ValueError:
        moved_subspace = False

    n_basic_indices = sum(index_types)

    # The number of dimensions in the subspace created by the advanced indices
    n_subspace_dims = max(
        (
            getattr(idx, "ndim", 0)
            for idx, is_basic in zip(full_indices, index_types)
            if not is_basic
        ),
        default=0,
    )

    # The number of dimensions for each expanded index
    n_output_dims = n_subspace_dims + n_basic_indices

    adv_indices = []
    shape_copy = list(shape)
    n_preceding_basics = 0
    for d, idx in enumerate(full_indices):
        if not is_basic_idx(idx):
            s = shape_copy.pop(0)

            idx = pt.as_tensor(idx)

            if moved_subspace:
                # The subspace generated by advanced indices appear as the
                # upper dimensions in the "expanded" index space, so we need to
                # add broadcast dimensions for the non-basic indices to the end
                # of these advanced indices
                expanded_idx = idx[(Ellipsis,) + (None,) * n_basic_indices]
            else:
                # In this case, we need to add broadcast dimensions for the
                # basic indices that proceed and follow the group of advanced
                # indices; otherwise, a contiguous group of advanced indices
                # forms a broadcasted set of indices that are iterated over
                # within the same subspace, which means that all their
                # corresponding "expanded" indices have exactly the same shape.
                expanded_idx = idx[(None,) * n_preceding_basics][
                    (Ellipsis,) + (None,) * (n_basic_indices - n_preceding_basics)
                ]
        else:
            if is_newaxis(idx):
                n_preceding_basics += 1
                continue

            s = shape_copy.pop(0)

            if isinstance(idx, slice) or isinstance(getattr(idx, "type", None), SliceType):
                idx = as_index_literal(idx)
                idx_slice, _ = get_canonical_form_slice(idx, s)
                idx = pt.arange(idx_slice.start, idx_slice.stop, idx_slice.step)

            if moved_subspace:
                # Basic indices appear in the lower dimensions
                # (i.e. right-most) in the output, and are preceded by
                # the subspace generated by the advanced indices.
                expanded_idx = idx[(None,) * (n_subspace_dims + n_preceding_basics)][
                    (Ellipsis,) + (None,) * (n_basic_indices - n_preceding_basics - 1)
                ]
            else:
                # In this case, we need to know when the basic indices have
                # moved past the contiguous group of advanced indices (in the
                # "expanded" index space), so that we can properly pad those
                # dimensions in this basic index's shape.
                # Don't forget that a single advanced index can introduce an
                # arbitrary number of dimensions to the expanded index space.

                # If we're currently at a basic index that's past the first
                # advanced index, then we're necessarily past the group of
                # advanced indices.
                n_preceding_dims = (
                    n_subspace_dims if d > first_adv_idx else 0
                ) + n_preceding_basics
                expanded_idx = idx[(None,) * n_preceding_dims][
                    (Ellipsis,) + (None,) * (n_output_dims - n_preceding_dims - 1)
                ]

            n_preceding_basics += 1

        assert expanded_idx.ndim <= n_output_dims

        adv_indices.append(expanded_idx)

    return cast(tuple[TensorVariable], tuple(pt.broadcast_arrays(*adv_indices)))


def rv_pull_down(x: TensorVariable) -> TensorVariable:
    """Pull a ``RandomVariable`` ``Op`` down through a graph, when possible."""
    fgraph = FunctionGraph(outputs=[x], clone=False, features=[ShapeFeature()])
    rewrites = [
        local_rv_size_lift,
        local_dimshuffle_rv_lift,
        local_subtensor_rv_lift,
        local_lift_DiracDelta,
    ]
    EquilibriumGraphRewriter(rewrites, max_use_ratio=100).rewrite(fgraph)
    return fgraph.outputs[0]


class MixtureRV(MeasurableOp, Op):
    """A placeholder used to specify a log-likelihood for a mixture sub-graph."""

    __props__ = ("indices_end_idx", "out_dtype", "out_broadcastable")

    def __init__(self, indices_end_idx, out_dtype, out_broadcastable):
        super().__init__()
        self.indices_end_idx = indices_end_idx
        self.out_dtype = out_dtype
        self.out_broadcastable = out_broadcastable

    def make_node(self, *inputs):
        return Apply(self, list(inputs), [TensorType(self.out_dtype, self.out_broadcastable)()])

    def perform(self, node, inputs, outputs):
        raise NotImplementedError("This is a stand-in Op.")  # pragma: no cover


def get_stack_mixture_vars(
    node: Apply,
) -> tuple[list[TensorVariable] | None, int | None]:
    r"""Extract the mixture terms from a `*Subtensor*` applied to stacked `MeasurableVariable`\s."""
    assert isinstance(node.op, subtensor_ops)

    joined_rvs = node.inputs[0]

    # First, make sure that it's some sort of concatenation
    if not (joined_rvs.owner and isinstance(joined_rvs.owner.op, MakeVector | Join)):
        return None, None

    if isinstance(joined_rvs.owner.op, MakeVector):
        join_axis = NoneConst
        mixture_rvs = joined_rvs.owner.inputs

    elif isinstance(joined_rvs.owner.op, Join):
        join_axis = joined_rvs.owner.inputs[0]
        # TODO: Support symbolic join axes. This will raise ValueError if it's not a constant
        (join_axis,) = constant_fold((join_axis,), raise_not_constant=False)
        join_axis = pt.as_tensor(join_axis, dtype="int64")

        mixture_rvs = joined_rvs.owner.inputs[1:]

    # Join and MakeVector can introduce PromisedValuedRV to prevent losing interdependencies
    mixture_rvs = [
        rv.owner.inputs[0] if rv.owner and isinstance(rv.owner.op, PromisedValuedRV) else rv
        for rv in mixture_rvs
    ]
    return mixture_rvs, join_axis


@node_rewriter(subtensor_ops)
def find_measurable_index_mixture(fgraph, node):
    r"""Identify mixture sub-graphs and replace them with a place-holder `Op`.

    The basic idea is to find ``stack(mixture_comps)[I_rv]``, where
    ``mixture_comps`` is a ``list`` of `MeasurableVariable`\s and ``I_rv`` is a
    `MeasurableVariable` with a discrete and finite support.
    From these terms, new terms ``Z_rv[i] = mixture_comps[i][i == I_rv]`` are
    created for each ``i`` in ``enumerate(mixture_comps)``.
    """
    mixing_indices = node.inputs[1:]

    # TODO: Add check / test case for Advanced Boolean indexing
    if isinstance(node.op, AdvancedSubtensor | AdvancedSubtensor1):
        # We don't support (non-scalar) integer array indexing as it can pick repeated values,
        # but the Mixture logprob assumes all mixture values are independent
        if any(
            indices.dtype.startswith("int") and sum(1 - b for b in indices.type.broadcastable) > 0
            for indices in mixing_indices
            if not isinstance(indices, SliceConstant)
        ):
            return None

    old_mixture_rv = node.default_output()
    mixture_rvs, join_axis = get_stack_mixture_vars(node)

    # We don't support symbolic join axis
    if mixture_rvs is None or not isinstance(join_axis, NoneTypeT | Constant):
        return None

    if set(filter_measurable_variables(mixture_rvs)) != set(mixture_rvs):
        return None

    # Replace this sub-graph with a `MixtureRV`
    mix_op = MixtureRV(
        1 + len(mixing_indices),
        old_mixture_rv.dtype,
        old_mixture_rv.broadcastable,
    )
    new_node = mix_op.make_node(*([join_axis, *mixing_indices, *mixture_rvs]))

    new_mixture_rv = new_node.default_output()

    if pytensor.config.compute_test_value != "off":
        # We can't use `MixtureRV` to compute a test value; instead, we'll use
        # the original node's test value.
        if not hasattr(old_mixture_rv.tag, "test_value"):
            compute_test_value(node)

        new_mixture_rv.tag.test_value = old_mixture_rv.tag.test_value

    return [new_mixture_rv]


@_logprob.register(MixtureRV)
def logprob_MixtureRV(op, values, *inputs: TensorVariable | slice | None, name=None, **kwargs):
    (value,) = values

    join_axis = cast(Variable, inputs[0])
    indices = cast(TensorVariable, inputs[1 : op.indices_end_idx])
    comp_rvs = cast(TensorVariable, inputs[op.indices_end_idx :])

    assert len(indices) > 0

    if len(indices) > 1 or indices[0].ndim > 0:
        if isinstance(join_axis.type, NoneTypeT):
            # `join_axis` will be `NoneConst` if the "join" was a `MakeVector`
            # (i.e. scalar measurable variables were combined to make a
            # vector).
            # Since some form of advanced indexing is necessarily occurring, we
            # need to reformat the MakeVector arguments so that they fit the
            # `Join` format expected by the logic below.
            join_axis_val = 0
            comp_rvs = [comp[None] for comp in comp_rvs]
            original_shape = (len(comp_rvs),)
        else:
            join_axis_val = constant_fold((join_axis,))[0].item()
            original_shape = shape_tuple(comp_rvs[0])

        bcast_indices = expand_indices(indices, original_shape)

        logp_val = pt.empty(bcast_indices[0].shape)

        for m, rv in enumerate(comp_rvs):
            idx_m_on_axis = pt.nonzero(pt.eq(bcast_indices[join_axis_val], m))
            m_indices = tuple(
                v[idx_m_on_axis] for i, v in enumerate(bcast_indices) if i != join_axis_val
            )
            # Drop superfluous join dimension
            rv = rv[0]
            # TODO: Do we really need to do this now?
            # Could we construct this form earlier and
            # do the lifting for everything at once, instead of
            # this intentional one-off?
            rv_m = rv_pull_down(rv[m_indices] if m_indices else rv)
            val_m = value[idx_m_on_axis]
            logp_m = _logprob_helper(rv_m, val_m)
            logp_val = pt.set_subtensor(logp_val[idx_m_on_axis], logp_m)

    else:
        # FIXME: This logprob implementation does not support mixing across distinct components,
        # but we sometimes use it, because MixtureRV does not keep information about at which
        # dimension scalar indexing actually starts

        # If the stacking operation expands the component RVs, we have
        # to expand the value and later squeeze the logprob for everything
        # to work correctly
        join_axis_val = None if isinstance(join_axis.type, NoneTypeT) else join_axis.data

        if join_axis_val is not None:
            value = pt.expand_dims(value, axis=join_axis_val)

        logp_val = 0.0
        for i, comp_rv in enumerate(comp_rvs):
            comp_logp = _logprob_helper(comp_rv, value)
            if join_axis_val is not None:
                comp_logp = pt.squeeze(comp_logp, axis=join_axis_val)
            logp_val += ifelse(
                pt.eq(indices[0], i),
                comp_logp,
                pt.zeros_like(comp_logp),
            )

    return logp_val


class MeasurableSwitchMixture(MeasurableElemwise):
    valid_scalar_types = (Switch,)


measurable_switch_mixture = MeasurableSwitchMixture(scalar_switch)


@node_rewriter([switch])
def find_measurable_switch_mixture(fgraph, node):
    if isinstance(node.op, MeasurableOp):
        return None

    switch_cond, *components = node.inputs

    # We don't support broadcasting of components, as that yields dependent (identical) values.
    # The current logp implementation assumes all component values are independent.
    # Broadcasting of the switch condition is fine
    out_bcast = node.outputs[0].type.broadcastable
    if any(comp.type.broadcastable != out_bcast for comp in components):
        return None

    if set(filter_measurable_variables(components)) != set(components):
        return None

    # Check that `switch_cond` is not potentially measurable
    if check_potential_measurability([switch_cond]):
        return None

    return [measurable_switch_mixture(switch_cond, *components)]


@_logprob.register(MeasurableSwitchMixture)
def logprob_switch_mixture(op, values, switch_cond, component_true, component_false, **kwargs):
    [value] = values

    return switch(
        switch_cond,
        _logprob_helper(component_true, value),
        _logprob_helper(component_false, value),
    )


measurable_ir_rewrites_db.register(
    "find_measurable_index_mixture",
    find_measurable_index_mixture,
    "basic",
    "mixture",
)

measurable_ir_rewrites_db.register(
    "find_measurable_switch_mixture",
    find_measurable_switch_mixture,
    "basic",
    "mixture",
)


class MeasurableIfElse(MeasurableOp, IfElse):
    """Measurable subclass of IfElse operator."""


@node_rewriter([IfElse])
def split_valued_ifelse(fgraph, node):
    """Split valued variables in multi-output ifelse into their own ifelse."""
    op = node.op

    if op.n_outs == 1:
        # Single outputs IfElse
        return None

    valued_output_nodes = get_related_valued_nodes(node, fgraph)
    if not valued_output_nodes:
        return None

    cond, *all_outputs = node.inputs
    then_outputs = all_outputs[: op.n_outs]
    else_outputs = all_outputs[op.n_outs :]

    # Split first topological valued output
    then_else_valued_outputs = []
    for valued_output_node in valued_output_nodes:
        rv, value = valued_output_node.inputs
        [valued_out] = valued_output_node.outputs
        rv_idx = node.outputs.index(rv)
        then_else_valued_outputs.append(
            (
                then_outputs[rv_idx],
                else_outputs[rv_idx],
                value,
                valued_out,
            )
        )

    toposort = fgraph.toposort()
    then_else_valued_outputs = sorted(
        then_else_valued_outputs,
        key=lambda x: max(toposort.index(x[0].owner), toposort.index(x[1].owner)),
    )

    (first_then, first_else, first_value_var, first_valued_out), *remaining_vars = (
        then_else_valued_outputs
    )
    first_ifelse = ifelse(cond, first_then, first_else)
    first_valued_ifelse = valued_rv(first_ifelse, first_value_var)
    replacements = {first_valued_out: first_valued_ifelse}

    if remaining_vars:
        first_ifelse_ancestors = {a for a in ancestors((first_then, first_else)) if a.owner}
        remaining_thens = [then_out for (then_out, _, _, _) in remaining_vars]
        remaininng_elses = [else_out for (_, else_out, _, _) in remaining_vars]
        if set(remaining_thens + remaininng_elses) & first_ifelse_ancestors:
            # IfElse graph cannot be split, because some remaining variables are inputs to first ifelse
            return None

        remaining_ifelses = ifelse(cond, remaining_thens, remaininng_elses)
        # Replace potential dependencies on first_then, first_else in remaining ifelse by first_valued_ifelse
        dummy_first_valued_ifelse = first_valued_ifelse.type()
        temp_fgraph = FunctionGraph(
            outputs=[*remaining_ifelses, dummy_first_valued_ifelse], clone=False
        )
        temp_fgraph.replace(first_then, dummy_first_valued_ifelse)
        temp_fgraph.replace(first_else, dummy_first_valued_ifelse)
        temp_fgraph.replace(dummy_first_valued_ifelse, first_valued_ifelse, import_missing=True)
        for remaining_ifelse, (_, _, remaining_value_var, remaining_valued_out) in zip(
            remaining_ifelses, remaining_vars
        ):
            remaining_valued_ifelse = valued_rv(remaining_ifelse, remaining_value_var)
            replacements[remaining_valued_out] = remaining_valued_ifelse

    return replacements


@node_rewriter([IfElse])
def find_measurable_ifelse_mixture(fgraph, node):
    """Find `IfElse` nodes that can be replaced by `MeasurableIfElse`."""
    op = node.op

    if isinstance(op, MeasurableOp):
        return None

    if op.n_outs > 1:
        # The rewrite split_measurable_ifelse should take care of this
        return None

    if_var, then_rv, else_rv = node.inputs

    if check_potential_measurability([if_var]):
        return None

    if len(filter_measurable_variables([then_rv, else_rv])) != 2:
        return None

    return MeasurableIfElse(n_outs=op.n_outs)(if_var, then_rv, else_rv, return_list=True)


early_measurable_ir_rewrites_db.register(
    "split_valued_ifelse",
    split_valued_ifelse,
    "basic",
    "mixture",
)

measurable_ir_rewrites_db.register(
    "find_measurable_ifelse_mixture",
    find_measurable_ifelse_mixture,
    "basic",
    "mixture",
)


@_logprob.register(MeasurableIfElse)
def logprob_ifelse(op, values, if_var, rv_then, rv_else, **kwargs):
    """Compute the log-likelihood graph for an `IfElse`."""
    [value] = values
    logps_then = _logprob_helper(rv_then, value, **kwargs)
    logps_else = _logprob_helper(rv_else, value, **kwargs)
    return ifelse(if_var, logps_then, logps_else)
