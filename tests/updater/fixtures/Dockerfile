FROM python:3.14-alpine

ARG BUILD_ID=fixture
ARG HEALTHY=1
ENV PYTHONPATH=/fixture \
    PLEX_MANAGER_BUILD_ID=${BUILD_ID} \
    FIXTURE_HEALTHY=${HEALTHY}

WORKDIR /fixture
COPY plex_manager ./plex_manager

EXPOSE 8000
HEALTHCHECK --interval=1s --timeout=1s --retries=2 \
    CMD python -c "import os,sys; sys.exit(0 if os.environ['FIXTURE_HEALTHY'] == '1' else 1)"
ENTRYPOINT ["python", "-m", "plex_manager"]
