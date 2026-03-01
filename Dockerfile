FROM bakayarou23/cita-checker:latest

COPY supervisord-cita-checker.conf /etc/supervisor/conf.d/cita-checker.conf

# Prebuild runtime deps once at image build time so startup is deterministic.
RUN python3 -m venv /home/nonroot/venv \
    && /home/nonroot/venv/bin/python3 -m ensurepip --upgrade \
    && /home/nonroot/venv/bin/python3 -m pip install --upgrade pip setuptools wheel \
    && /home/nonroot/venv/bin/python3 -m pip install seleniumbase webdriver-manager \
    && chown -R nonroot:nonroot /home/nonroot/venv
