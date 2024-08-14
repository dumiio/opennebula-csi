FROM python:3.10-slim-bookworm

WORKDIR /app

COPY requirements.txt requirements.txt
RUN pip3 install -r requirements.txt

COPY . .

RUN ["python3", "-m", "grpc_tools.protoc", "-I./protos", "--python_out=./pb/", "--grpc_python_out=./pb/", "./protos/csi.proto"]

RUN sed -i "/import csi_pb2 as csi__pb2/c\from . import csi_pb2 as csi__pb2" ./pb/csi_pb2_grpc.py

ADD https://github.com/Yelp/dumb-init/releases/download/v1.2.5/dumb-init_1.2.5_x86_64 /usr/sbin/dumb-init
RUN chmod +x /usr/sbin/dumb-init

ENTRYPOINT ["/usr/sbin/dumb-init", "--"]

CMD [ "python3", "server.py" ]
