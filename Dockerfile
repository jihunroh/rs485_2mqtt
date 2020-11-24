FROM python:3.9.0-slim
WORKDIR /app 
COPY . /app  
RUN pip install --trusted-host pypi.python.org -r requirements.txt 
EXPOSE 80 
CMD ["python", "rs485_2mqtt.py"]
