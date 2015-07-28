FROM python:2.7-onbuild

EXPOSE 25

CMD ["smtpbroker.py"]

RUN python setup.py install
