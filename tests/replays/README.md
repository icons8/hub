# Failure replays

Each file is a deterministic scenario: events in order (`t` is seconds from the start) and the expected result.
`expect.sessions` / `expect.messages` are compared by the listed fields, `expect.actions` is the whole list of actions
in the order they appear, by the listed keys. `expect.rejected` is how many events the core rejected.
The runner is `tests/test_replays.py`; there is no model and no tmux.
An earlier form of the core's text (a label, a sender id) is written as `⟦legacy.<key>[.<subkey>]⟧` and expanded on load
from `core/langdata.legacy` (`tests/test_replays.load`, the one reader of these files); an unknown placeholder fails.
