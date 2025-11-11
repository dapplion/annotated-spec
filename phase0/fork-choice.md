# Ethereum 2.0 Phase 0 -- Beacon Chain Fork Choice

**Notice**: This document was written in Aug 2020.

## Table of contents
<!-- TOC -->
<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->


- [Introduction](#introduction)
- [Fork choice](#fork-choice)
  - [Configuration](#configuration)
  - [Helpers](#helpers)
    - [`LatestMessage`](#latestmessage)
    - [`Store`](#store)
    - [`get_forkchoice_store`](#get_forkchoice_store)
    - [`get_slots_since_genesis`](#get_slots_since_genesis)
    - [`get_current_slot`](#get_current_slot)
    - [`compute_slots_since_epoch_start`](#compute_slots_since_epoch_start)
    - [`get_ancestor`](#get_ancestor)
    - [`get_latest_attesting_balance`](#get_latest_attesting_balance)
    - [`filter_block_tree`](#filter_block_tree)
    - [`get_filtered_block_tree`](#get_filtered_block_tree)
    - [`get_head`](#get_head)
    - [`should_update_justified_checkpoint`](#should_update_justified_checkpoint)
    - [`on_attestation` helpers](#on_attestation-helpers)
      - [`validate_on_attestation`](#validate_on_attestation)
      - [`store_target_checkpoint_state`](#store_target_checkpoint_state)
      - [`update_latest_messages`](#update_latest_messages)
  - [Handlers](#handlers)
    - [`on_tick`](#on_tick)
    - [`on_block`](#on_block)
    - [`on_attestation`](#on_attestation)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->
<!-- /TOC -->

## Introduction

This document is the beacon chain fork choice spec, part of Phase 0. It assumes
the [beacon chain state transition function spec](./beacon-chain.md).

### Checkpoints vs blocks

Note also that Casper FFG deals with **checkpoints** and the **checkpoint tree**, whereas LMD GHOST deals with blocks and the **block tree**. One simple way to think about this is that the checkpoint tree is a compressed version of the block tree, where we only consider blocks at the start of an epoch, and where checkpoint A is a parent of checkpoint B if block A is the ancestor of B in the block tree that begins the epoch before B:

![](../images/checkpoint_tree_1.png)

_Green blocks are the checkpoints; in the checkpoint tree, A is the parent of B1 and B2._

But to approach a more realistic picture we need to account for an important scenario: when there are skipped slots. If a chain has a skipped block at some slot, we take the most recent block before that slot in that chain as the epoch boundary block:

![](../images/checkpoint_tree_2.png)

In any case, where there is a divergence between multiple chains, skipped slots will be frequent because proposer selection and slashing rules forbid multiple blocks from being created in the same slot (unless two chains diverge for more than four epochs)! Note also that if we want to compute the _post-state_ of a checkpoint, we would have to run [the `process_slots` function](https://github.com/ethereum/annotated-spec/blob/master/phase0/beacon-chain.md#state-transition) up to the first slot of the epoch to process the empty slots.

Another important edge case is when there is more than an entire epoch of skipped slots. To cover this edge case, we think about a checkpoint as a (block, slot) pair, not a blocks; this means that the same block could be part of multiple checkpoints that an attestation could validly reference:

![](../images/checkpoint_tree_3.png)

In this case, the block B2 corresponds to two checkpoints, `(B2, N)` and `(B2, N+1)`, one for each epoch. In general, the most helpful model for understanding Ethereum state transitions may actually be the full state transition tree, where we look at all possible `(block, slot)` pairs:

![](../images/checkpoint_tree_4.png)

If you're a mathematician, you could view both the block tree and the checkpoint tree as [graph minors](https://en.wikipedia.org/wiki/Graph_minor) of the state transition tree. Casper FFG works over the checkpoint tree, and LMD GHOST works over the block tree. For convenience, we'll use the phrase "**latest justified block**" (LJB) to refer to the block referenced in the latest justified checkpoint; because LMD GHOST works over the block tree we'll keep the discussion to blocks, though the actual data structures will talk about the latest justified checkpoint. Note that while one block may map to multiple checkpoints, a checkpoint only references one block, so this language is unambiguous.

Now, let's go through the specification...

## Fork choice

The head block root associated with a `store` is defined as `get_head(store)`.
At genesis, let `store = get_forkchoice_store(genesis_state, genesis_block)` and
update `store` by running:

- `on_tick(store, time)` whenever `time > store.time` where `time` is the
  current Unix time
- `on_block(store, block)` whenever a block `block: SignedBeaconBlock` is
  received
- `on_attestation(store, attestation)` whenever an attestation `attestation` is
  received
- `on_attester_slashing(store, attester_slashing)` whenever an attester slashing
  `attester_slashing` is received

Any of the above handlers that trigger an unhandled exception (e.g. a failed
assert or an out-of-range list access) are considered invalid. Invalid calls to
handlers must not modify `store`.

*Notes*:

1. **Leap seconds**: Slots will last `SECONDS_PER_SLOT + 1` or
   `SECONDS_PER_SLOT - 1` seconds around leap seconds. This is automatically
   handled by [UNIX time](https://en.wikipedia.org/wiki/Unix_time).
2. **Honest clocks**: Honest nodes are assumed to have clocks synchronized
   within `SECONDS_PER_SLOT` seconds of each other.
3. **Eth1 data**: The large `ETH1_FOLLOW_DISTANCE` specified in the
   [honest validator document](./validator.md) should ensure that
   `state.latest_eth1_data` of the canonical beacon chain remains consistent
   with the canonical Ethereum proof-of-work chain. If not, emergency manual
   intervention will be required.
4. **Manual forks**: Manual forks may arbitrarily change the fork choice rule
   but are expected to be enacted at epoch transitions, with the fork details
   reflected in `state.fork`.
5. **Implementation**: The implementation found in this specification is
   constructed for ease of understanding rather than for optimization in
   computation, space, or any other resource. A number of optimized alternatives
   can be found [here](https://github.com/protolambda/lmd-ghost).

### Configuration

| Name                                  | Value         |
| ------------------------------------- | ------------- |
| `PROPOSER_SCORE_BOOST`                | `uint64(40)`  |
| `REORG_HEAD_WEIGHT_THRESHOLD`         | `uint64(20)`  |
| `REORG_PARENT_WEIGHT_THRESHOLD`       | `uint64(160)` |
| `REORG_MAX_EPOCHS_SINCE_FINALIZATION` | `Epoch(2)`    |

- The proposer score boost and re-org weight threshold are percentage values
  that are measured with respect to the weight of a single committee. See
  `calculate_committee_fraction`.

### Helpers

#### `LatestMessage`

```python
@dataclass(eq=True, frozen=True)
class LatestMessage(object):
    epoch: Epoch
    root: Root
```

#### `Store`

The `Store` is responsible for tracking information required for the fork choice
algorithm. The important fields being tracked are described below:

- `justified_checkpoint`: the justified checkpoint used as the starting point
  for the LMD GHOST fork choice algorithm.
- `finalized_checkpoint`: the highest known finalized checkpoint. The fork
  choice only considers blocks that are not conflicting with this checkpoint.
- `unrealized_justified_checkpoint` & `unrealized_finalized_checkpoint`: these
  track the highest justified & finalized checkpoints resp., without regard to
  whether on-chain ***realization*** has occurred, i.e. FFG processing of new
  attestations within the state transition function. This is an important
  distinction from `justified_checkpoint` & `finalized_checkpoint`, because they
  will only track the checkpoints that are realized on-chain. Note that on-chain
  processing of FFG information only happens at epoch boundaries.
- `unrealized_justifications`: stores a map of block root to the unrealized
  justified checkpoint observed in that block.

```python
@dataclass
class Store(object):
    time: uint64
    genesis_time: uint64
    justified_checkpoint: Checkpoint
    finalized_checkpoint: Checkpoint
    unrealized_justified_checkpoint: Checkpoint
    unrealized_finalized_checkpoint: Checkpoint
    proposer_boost_root: Root
    equivocating_indices: Set[ValidatorIndex]
    blocks: Dict[Root, BeaconBlock] = field(default_factory=dict)
    block_states: Dict[Root, BeaconState] = field(default_factory=dict)
    block_timeliness: Dict[Root, boolean] = field(default_factory=dict)
    checkpoint_states: Dict[Checkpoint, BeaconState] = field(default_factory=dict)
    latest_messages: Dict[ValidatorIndex, LatestMessage] = field(default_factory=dict)
    unrealized_justifications: Dict[Root, Checkpoint] = field(default_factory=dict)
```

#### `get_forkchoice_store`

The provided anchor-state will be regarded as a trusted state, to not roll back
beyond. This should be the genesis state for a full client.

*Note* With regards to fork choice, block headers are interchangeable with
blocks. The spec is likely to move to headers for reduced overhead in test
vectors and better encapsulation. Full implementations store blocks as part of
their database and will often use full blocks when dealing with production fork
choice.

```python
def get_forkchoice_store(anchor_state: BeaconState, anchor_block: BeaconBlock) -> Store:
    assert anchor_block.state_root == hash_tree_root(anchor_state)
    anchor_root = hash_tree_root(anchor_block)
    anchor_epoch = get_current_epoch(anchor_state)
    justified_checkpoint = Checkpoint(epoch=anchor_epoch, root=anchor_root)
    finalized_checkpoint = Checkpoint(epoch=anchor_epoch, root=anchor_root)
    proposer_boost_root = Root()
    return Store(
        time=uint64(anchor_state.genesis_time + SECONDS_PER_SLOT * anchor_state.slot),
        genesis_time=anchor_state.genesis_time,
        justified_checkpoint=justified_checkpoint,
        finalized_checkpoint=finalized_checkpoint,
        unrealized_justified_checkpoint=justified_checkpoint,
        unrealized_finalized_checkpoint=finalized_checkpoint,
        proposer_boost_root=proposer_boost_root,
        equivocating_indices=set(),
        blocks={anchor_root: copy(anchor_block)},
        block_states={anchor_root: copy(anchor_state)},
        checkpoint_states={justified_checkpoint: copy(anchor_state)},
        unrealized_justifications={anchor_root: justified_checkpoint},
    )
```

#### `get_slots_since_genesis`

```python
def get_slots_since_genesis(store: Store) -> int:
    return (store.time - store.genesis_time) // SECONDS_PER_SLOT
```

#### `get_current_slot`

```python
def get_current_slot(store: Store) -> Slot:
    return Slot(GENESIS_SLOT + get_slots_since_genesis(store))
```

#### `compute_slots_since_epoch_start`

```python
def compute_slots_since_epoch_start(slot: Slot) -> int:
    return slot - compute_start_slot_at_epoch(compute_epoch_at_slot(slot))
```

#### `get_ancestor`

```python
def get_ancestor(store: Store, root: Root, slot: Slot) -> Root:
    block = store.blocks[root]
    if block.slot > slot:
        return get_ancestor(store, block.parent_root, slot)
    return root
```

#### `get_latest_attesting_balance`

```python
def get_latest_attesting_balance(store: Store, root: Root) -> Gwei:
    state = store.checkpoint_states[store.justified_checkpoint]
    active_indices = get_active_validator_indices(state, get_current_epoch(state))
    return Gwei(sum(
        state.validators[i].effective_balance for i in active_indices
        if (i in store.latest_messages
            and get_ancestor(store, store.latest_messages[i].root, store.blocks[root].slot) == root)
    ))
```

Get the total ETH attesting to a given block or its descendants, considering only latest attestations and active validators. This is the main function that is used to choose between two children of a block in LMD GHOST. Recall the diagram from above:

![](https://vitalik.ca/files/posts_files/cbc-casper-files/Chain7.png)

In this diagram, we assume that each of the last five block proposals (the blue ones) carries one attestation, which specifies that block as the head, and we assume each block is created by a different validator, and all validators have the same deposit size. The number in each square represents the latest attesting balance of that block. In eth2, blocks and attestations are separate, and there will be hundreds of attestations supporting each block, but otherwise, the principle is the same.

#### `filter_block_tree`

*Note*: External calls to `filter_block_tree` (i.e., any calls that are not made
by the recursive logic in this function) MUST set `block_root` to
`store.justified_checkpoint.root`.

```python
def filter_block_tree(store: Store, block_root: Root, blocks: Dict[Root, BeaconBlock]) -> bool:
    block = store.blocks[block_root]
    children = [
        root for root in store.blocks.keys() if store.blocks[root].parent_root == block_root
    ]

    # If any children branches contain expected finalized/justified checkpoints,
    # add to filtered block-tree and signal viability to parent.
    if any(children):
        filter_block_tree_result = [filter_block_tree(store, child, blocks) for child in children]
        if any(filter_block_tree_result):
            blocks[block_root] = block
            return True
        return False

    current_epoch = get_current_store_epoch(store)
    voting_source = get_voting_source(store, block_root)

    # The voting source should be either at the same height as the store's justified checkpoint or
    # not more than two epochs ago
    correct_justified = (
        store.justified_checkpoint.epoch == GENESIS_EPOCH
        or voting_source.epoch == store.justified_checkpoint.epoch
        or voting_source.epoch + 2 >= current_epoch
    )

    finalized_checkpoint_block = get_checkpoint_block(
        store,
        block_root,
        store.finalized_checkpoint.epoch,
    )

    correct_finalized = (
        store.finalized_checkpoint.epoch == GENESIS_EPOCH
        or store.finalized_checkpoint.root == finalized_checkpoint_block
    )

    # If expected finalized/justified, add to viable block-tree and signal viability to parent.
    if correct_justified and correct_finalized:
        blocks[block_root] = block
        return True

    # Otherwise, branch not viable
    return False
```

#### `get_filtered_block_tree`

```python
def get_filtered_block_tree(store: Store) -> Dict[Root, BeaconBlock]:
    """
    Retrieve a filtered block tree from ``store``, only returning branches
    whose leaf state's justified/finalized info agrees with that in ``store``.
    """
    base = store.justified_checkpoint.root
    blocks: Dict[Root, BeaconBlock] = {}
    filter_block_tree(store, base, blocks)
    return blocks
```

#### `get_head`

```python
def get_head(store: Store) -> Root:
    # Get filtered block tree that only includes viable branches
    blocks = get_filtered_block_tree(store)
    # Execute the LMD-GHOST fork choice
    head = store.justified_checkpoint.root
    while True:
        children = [root for root in blocks.keys() if blocks[root].parent_root == head]
        if len(children) == 0:
            return head
        # Sort by latest attesting balance with ties broken lexicographically
        # Ties broken by favoring block with lexicographically higher root
        head = max(children, key=lambda root: (get_weight(store, root), root))
```

#### `should_update_justified_checkpoint`

```python
def should_update_justified_checkpoint(store: Store, new_justified_checkpoint: Checkpoint) -> bool:
    """
    To address the bouncing attack, only update conflicting justified
    checkpoints in the fork choice if in the early slots of the epoch.
    Otherwise, delay incorporation of new justified checkpoint until next epoch boundary.

    See https://ethresear.ch/t/prevention-of-bouncing-attack-on-ffg/6114 for more detailed analysis and discussion.
    """
    if compute_slots_since_epoch_start(get_current_slot(store)) < SAFE_SLOTS_TO_UPDATE_JUSTIFIED:
        return True

    justified_slot = compute_start_slot_at_epoch(store.justified_checkpoint.epoch)
    if not get_ancestor(store, new_justified_checkpoint.root, justified_slot) == store.justified_checkpoint.root:
        return False

    return True
```

The idea here is that we want to only change the last-justified-block within the first 1/3 of an epoch. This prevents "bouncing attacks" of the following form:

1. Start from a scenario wherein epoch N, 62% of validators support block A, and in epoch N+1, 62% of validators support block B. Suppose that the attacker has 5% of the total stake. This scenario requires very exceptional networking conditions to get into; the point of the attack, however, is that if we get into such a scenario the attacker could perpetuate it, permanently preventing finality.
2. Due to LMD GHOST, B is favored, and so validators are continuing to vote for B. However, the attacker suddenly publishes attestations worth 5% of the total stake tagged with epoch N for block A, causing A to get justified.
3. In epoch N+2, A is justified and so validators are attesting to A', a descendant of A. When A' gets to 62% support, the attacker publishes attestations worth 5% of total stake for B. Now B is justified and favored by the fork choice.
4. In epoch N+3, B is justified, and so validators are attesting to B', a descendant of B. When B' gets to 62% support, the attacker publishes attestations worth 5% of total stake for A'...

This could continue forever, bouncing permanently between the two chains preventing any new block from being finalized. This attack can happen because the combined use of LMD GHOST and Casper FFG creates a discontinuity, where a small shift in support for a block can outweigh a large amount of support for another block if that small shift pushes it past the 2/3 threshold needed for justification. We block the attack by only allowing the latest justified block to change near the beginning of an epoch; this way, there is a full 2/3 of an epoch during which honest validators agree on the head and have the opportunity to justify a block and thereby further cement it, at the same time causing the LMD GHOST rule to strongly favor that head. This sets up that block to most likely be finalized in the next epoch.

See [Ryuya Nakamura's ethresear.ch post](https://ethresear.ch/t/prevention-of-bouncing-attack-on-ffg/6114) for more discussion.

#### `on_attestation` helpers

##### `validate_on_attestation`

```python
def validate_on_attestation(store: Store, attestation: Attestation, is_from_block: bool) -> None:
    target = attestation.data.target

    # If the given attestation is not from a beacon block message, we have to check the target epoch scope.
    if not is_from_block:
        validate_target_epoch_against_current_time(store, attestation)

    # Check that the epoch number and slot number are matching
    assert target.epoch == compute_epoch_at_slot(attestation.data.slot)

    # Attestation target must be for a known block. If target block is unknown, delay consideration until block is found
    assert target.root in store.blocks

    # Attestations must be for a known block. If block is unknown, delay consideration until the block is found
    assert attestation.data.beacon_block_root in store.blocks
    # Attestations must not be for blocks in the future. If not, the attestation should not be considered
    assert store.blocks[attestation.data.beacon_block_root].slot <= attestation.data.slot

    # LMD vote must be consistent with FFG vote target
    assert target.root == get_checkpoint_block(
        store, attestation.data.beacon_block_root, target.epoch
    )

    # Attestations can only affect the fork choice of subsequent slots.
    # Delay consideration in the fork choice until their slot is in the past.
    assert get_current_slot(store) >= attestation.data.slot + 1
```

##### `store_target_checkpoint_state`

```python
def store_target_checkpoint_state(store: Store, target: Checkpoint) -> None:
    # Store target checkpoint state if not yet seen
    if target not in store.checkpoint_states:
        base_state = copy(store.block_states[target.root])
        if base_state.slot < compute_start_slot_at_epoch(target.epoch):
            process_slots(base_state, compute_start_slot_at_epoch(target.epoch))
        store.checkpoint_states[target] = base_state
```

##### `update_latest_messages`

```python
def update_latest_messages(
    store: Store, attesting_indices: Sequence[ValidatorIndex], attestation: Attestation
) -> None:
    target = attestation.data.target
    beacon_block_root = attestation.data.beacon_block_root
    non_equivocating_attesting_indices = [
        i for i in attesting_indices if i not in store.equivocating_indices
    ]
    for i in non_equivocating_attesting_indices:
        if i not in store.latest_messages or target.epoch > store.latest_messages[i].epoch:
            store.latest_messages[i] = LatestMessage(epoch=target.epoch, root=beacon_block_root)
```

### Handlers

#### `on_tick`

```python
def on_tick(store: Store, time: uint64) -> None:
    # If the ``store.time`` falls behind, while loop catches up slot by slot
    # to ensure that every previous slot is processed with ``on_tick_per_slot``
    tick_slot = (time - store.genesis_time) // SECONDS_PER_SLOT
    while get_current_slot(store) < tick_slot:
        previous_time = store.genesis_time + (get_current_slot(store) + 1) * SECONDS_PER_SLOT
        on_tick_per_slot(store, previous_time)
    on_tick_per_slot(store, time)
```

#### `on_block`

```python
def on_block(store: Store, signed_block: SignedBeaconBlock) -> None:
    block = signed_block.message
    # Parent block must be known
    assert block.parent_root in store.block_states
    # Make a copy of the state to avoid mutability issues
    pre_state = copy(store.block_states[block.parent_root])
    # Blocks cannot be in the future. If they are, their consideration must be delayed until they are in the past.
    assert get_current_slot(store) >= block.slot

    # Check that block is later than the finalized epoch slot (optimization to reduce calls to get_ancestor)
    finalized_slot = compute_start_slot_at_epoch(store.finalized_checkpoint.epoch)
    assert block.slot > finalized_slot
    # Check block is a descendant of the finalized block at the checkpoint finalized slot
    finalized_checkpoint_block = get_checkpoint_block(
        store,
        block.parent_root,
        store.finalized_checkpoint.epoch,
    )
    assert store.finalized_checkpoint.root == finalized_checkpoint_block

    # Check the block is valid and compute the post-state
    state = pre_state.copy()
    block_root = hash_tree_root(block)
    state_transition(state, signed_block, True)
    # Add new block to the store
    store.blocks[block_root] = block
    # Add new state for this block to the store
    store.block_states[block_root] = state

    # Add block timeliness to the store
    seconds_since_genesis = store.time - store.genesis_time
    time_into_slot_ms = seconds_to_milliseconds(seconds_since_genesis) % SLOT_DURATION_MS
    epoch = get_current_store_epoch(store)
    attestation_threshold_ms = get_attestation_due_ms(epoch)
    is_before_attesting_interval = time_into_slot_ms < attestation_threshold_ms
    is_timely = get_current_slot(store) == block.slot and is_before_attesting_interval
    store.block_timeliness[hash_tree_root(block)] = is_timely

    # Add proposer score boost if the block is timely and not conflicting with an existing block
    is_first_block = store.proposer_boost_root == Root()
    if is_timely and is_first_block:
        store.proposer_boost_root = hash_tree_root(block)

    # Update checkpoints in store if necessary
    update_checkpoints(store, state.current_justified_checkpoint, state.finalized_checkpoint)

    # Eagerly compute unrealized justification and finality
    compute_pulled_up_tip(store, block_root)
```

#### `on_attestation`

```python
def on_attestation(store: Store, attestation: Attestation, is_from_block: bool = False) -> None:
    """
    Run ``on_attestation`` upon receiving a new ``attestation`` from either within a block or directly on the wire.

    An ``attestation`` that is asserted as invalid may be valid at a later time,
    consider scheduling it for later processing in such case.
    """
    validate_on_attestation(store, attestation, is_from_block)

    store_target_checkpoint_state(store, attestation.data.target)

    # Get state at the `target` to fully validate attestation
    target_state = store.checkpoint_states[attestation.data.target]
    indexed_attestation = get_indexed_attestation(target_state, attestation)
    assert is_valid_indexed_attestation(target_state, indexed_attestation)

    # Update latest messages for attesting indices
    update_latest_messages(store, indexed_attestation.attesting_indices, attestation)
```

