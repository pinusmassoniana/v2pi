# Vendored xray StatsService gRPC stubs

`command_pb2.py` / `command_pb2_grpc.py` are generated from `command.proto`
(reconstructed from xray-core `app/stats/command/command.proto`; pinned target
**xray 26.3.27**). They are committed so the panel needs no codegen at runtime.

Regenerate after changing `command.proto`:

```bash
cd backend
uv run python -m grpc_tools.protoc -I pi_gw_panel/stats/proto \
    --python_out=pi_gw_panel/stats/proto \
    --grpc_python_out=pi_gw_panel/stats/proto \
    pi_gw_panel/stats/proto/command.proto
```

Then re-apply the one vendoring fix in `command_pb2_grpc.py` (protoc emits an
absolute import that doesn't resolve inside the package):

```python
from pi_gw_panel.stats.proto import command_pb2 as command__pb2
```
