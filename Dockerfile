FROM bakayarou23/cita-checker:latest

COPY supervisord-cita-checker.conf /etc/supervisor/conf.d/cita-checker.conf
