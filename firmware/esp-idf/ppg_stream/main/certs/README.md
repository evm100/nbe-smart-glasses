# Node credentials (not in git)

`firmware/tools/nbe_node.py configure <NODE_ID>` writes these files:

| file | what |
|---|---|
| `client.crt` | the node's certificate, CN = node ID, signed by the NBE Device CA |
| `client.key` | its private key – **never commit or share**; anyone with it can pose as this node |
| `broker_ca.crt` | only if the broker uses a private CA (default: public CA via the ESP-IDF bundle) |

They are embedded into the firmware at build time (USB/UART builds don't need them).
Everything in this folder except this README is ignored by git.
