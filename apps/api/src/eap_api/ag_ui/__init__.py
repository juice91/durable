"""The AG-UI output adapter (docs/plans/2026-11-13-milestone-ag-ui.md).

The only package of the API that may import `ag_ui` (D19). Submodules: `input` (AGUI.5: the
`RunAgentInput` mapping onto the shared run-start path); `translate` (AGUI.4: `AgUiFormatter`, the
native items -> AG-UI SSE frames), `snapshot` (AGUI.4: D13 messages from durable data) and
`conformance` (AGUI.4: frame parsing, schema and lifecycle checks for tests).
"""
