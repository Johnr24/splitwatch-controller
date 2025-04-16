import paho-mqtt as mqtt
import time
from itertools import cycle

# MQTT broker details
broker_address = "192.168.69.213"
broker_port = 1883
username = "fuck123"
password = "fuck123"
topic = "homeassistant/text/splitflap/state"


#create a stopwatch that counts up and sends the time to the broker#create a stopwatch that counts up and sends the time to the broker

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("Connected to broker")
    else:
        print("Connection failed with code", rc)

def on_publish(client, userdata, mid):
    print("Message published")

client = mqtt.Client()
client.username_pw_set(username, password)
client.on_connect = on_connect
client.on_publish = on_publish

client.connect(broker_address, broker_port, 60)
client.loop_start()

start_time = time.time()

try:
    while True:
        elapsed_time = time.time() - start_time
        formatted_time = time.strftime("%H:%M:%S", time.gmtime(elapsed_time))
        client.publish(topic, formatted_time)
        time.sleep(1)
except KeyboardInterrupt:
    print("Stopwatch stopped")
    client.loop_stop()
    client.disconnect()