## `backend/certs/`

This repository previously included TLS certificate/key material for terminating HTTPS on the EC2 host.

For AWS deployments we are using **ALB + ACM**, so:
- TLS terminates at the **ALB**
- Containers should serve **HTTP** only (ALB forwards to the EC2 instance / container port)
- **Do not store private keys/certificates in this repo**

If you need local HTTPS for development, generate local-only certificates and keep them out of git.

