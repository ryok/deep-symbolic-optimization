FROM nvcr.io/nvidia/tensorflow:19.11-tf1-py3

ADD requirements.txt /tmp
ADD requirements_control.txt /tmp
RUN pip install -r /tmp/requirements.txt
RUN pip install -r /tmp/requirements_control.txt
