FROM python:2.7-onbuild

EXPOSE 25

CMD ["broker-smtp.py"]

RUN python setup.py install
