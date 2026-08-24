ARG TRINITY_BASE_IMAGE=trinity-agent-base:acp-interop
FROM ${TRINITY_BASE_IMAGE}

ARG HERMES_REF

USER root
RUN test -n "${HERMES_REF}" && \
    git clone --filter=blob:none https://github.com/NousResearch/hermes-agent.git /opt/hermes-agent && \
    git -C /opt/hermes-agent checkout --detach "${HERMES_REF}" && \
    python3 -m venv /opt/hermes-venv && \
    /opt/hermes-venv/bin/pip install --no-cache-dir --upgrade pip && \
    /opt/hermes-venv/bin/pip install --no-cache-dir -e '/opt/hermes-agent[acp]' && \
    /opt/hermes-venv/bin/python -c \
      "import acp; assert acp.PROTOCOL_VERSION == 1; print('Hermes ACP SDK isolated')"

RUN install -d -o developer -g developer /home/developer/.hermes /opt/trinity-interop
COPY --chown=developer:developer images/hermes-config.yaml /home/developer/.hermes/config.yaml
COPY --chown=developer:developer images/hermes-config.yaml /opt/trinity-interop/hermes-config.yaml
COPY --chown=developer:developer run_provider_smoke.py /opt/trinity-interop/run_provider_smoke.py

ENV AGENT_RUNTIME=acp
ENV AGENT_RUNTIME_COMMAND=/opt/hermes-venv/bin/hermes
ENV AGENT_RUNTIME_ARGS='["acp"]'
ENV TRINITY_ACP_INTEROP_AGENT=hermes

USER developer
WORKDIR /workspace
