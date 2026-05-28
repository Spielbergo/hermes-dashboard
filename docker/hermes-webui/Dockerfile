FROM ghcr.io/nesquena/hermes-webui:latest

# Switch to root to install system dependencies
USER root

# Install Playwright browsers to a world-readable shared location
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Install Playwright with all system dependencies (runs apt-get internally)
RUN pip install playwright && python -m playwright install --with-deps chromium
# Ensure the browser directory is accessible to all users
RUN chmod -R o+rx /ms-playwright

# Switch back to the non-privileged user
USER hermeswebui

# Carry the env var forward so hermeswebui picks up the shared browser path
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
