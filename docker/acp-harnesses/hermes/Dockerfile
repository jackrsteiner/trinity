ARG TRINITY_BASE_IMAGE=trinity-agent-base:latest
FROM ${TRINITY_BASE_IMAGE}

USER root

# Pin the acceptance harness so rebuilding the image cannot silently change
# the ACP wire behavior under Trinity.
RUN python3 -m pip install --no-cache-dir 'hermes-agent[acp]==0.19.0'

RUN mkdir -p /opt/trinity/acp
COPY runtime.json /opt/trinity/acp/runtime.json
COPY launcher.sh /opt/trinity/acp/launch
COPY hermes_compat.py /opt/trinity/acp/hermes_compat.py
RUN chown root:root /opt/trinity/acp/runtime.json /opt/trinity/acp/launch \
        /opt/trinity/acp/hermes_compat.py && \
    chmod 0444 /opt/trinity/acp/runtime.json && \
    chmod 0444 /opt/trinity/acp/hermes_compat.py && \
    chmod 0555 /opt/trinity/acp/launch

ENV AGENT_RUNTIME=acp
USER developer

CMD ["/app/startup.sh"]
