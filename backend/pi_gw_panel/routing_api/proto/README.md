# Vendored xray RoutingService gRPC stubs (A5)

The file is `router_command.proto`, not `command.proto`: protobuf registers descriptors by
FILE NAME in one global pool, and the stats stubs already own that name — importing both
raises `duplicate file name command.proto` at import time.

`router_command_pb2.py` / `router_command_pb2_grpc.py` / `network_pb2.py` are generated from the two `.proto`
files here, reconstructed from xray-core `app/router/command/router_command.proto` and the one enum it
imports; pinned target **xray 26.3.27**. Committed so the panel needs no codegen at runtime.

Regenerate after changing either proto:

```bash
cd backend
uv run python -m grpc_tools.protoc -I pi_gw_panel/routing_api/proto \
    --python_out=pi_gw_panel/routing_api/proto \
    --grpc_python_out=pi_gw_panel/routing_api/proto \
    pi_gw_panel/routing_api/proto/router_command.proto pi_gw_panel/routing_api/proto/network.proto
```

Then re-apply the two vendoring fixes protoc's absolute imports need, and delete the unused
`network_pb2_grpc.py`:

```python
# router_command_pb2.py
from pi_gw_panel.routing_api.proto import network_pb2 as network__pb2
# router_command_pb2_grpc.py
from pi_gw_panel.routing_api.proto import router_command_pb2 as command__pb2
```
