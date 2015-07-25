FROM python:2.7-onbuild

EXPOSE 25

CMD ["smtpd.py"]

RUN python setup.py install
