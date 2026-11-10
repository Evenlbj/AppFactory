FROM grafana/grafana:13.2.5-distroless-slim

ENV GF_SECURITY_ADMIN_PASSWORD=admin
ENV GF_USERS_ALLOW_SIGN_UP=false

COPY provisioning/ /etc/grafana/provisioning/

EXPOSE 3000
