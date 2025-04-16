import os
import paho.mqtt.client as paho_mqtt
import logging

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

class MQTTHandler:
    """Handles MQTT connection and publishing."""

    def __init__(self, broker_address, broker_port, topic, username=None, password=None):
        """Initializes the MQTT client."""
        self.broker_address = broker_address
        self.broker_port = int(broker_port)
        self.topic = topic
        self.username = username
        self.password = password
        self.client = paho_mqtt.Client(paho_mqtt.CallbackAPIVersion.VERSION1)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_publish = self._on_publish

        if self.username and self.password:
            self.client.username_pw_set(self.username, self.password)

    def _on_connect(self, client, userdata, flags, rc):
        """Callback for when the client connects to the MQTT broker."""
        if rc == 0:
            logger.info(f"Successfully connected to MQTT Broker at {self.broker_address}:{self.broker_port}")
        else:
            logger.error(f"Failed to connect to MQTT Broker, return code {rc}")

    def _on_disconnect(self, client, userdata, rc):
        """Callback for when the client disconnects from the MQTT broker."""
        logger.info("Disconnected from MQTT Broker.")
        if rc != 0:
            logger.warning(f"Unexpected MQTT disconnection. Will auto-reconnect: {rc}")


    def _on_publish(self, client, userdata, mid):
        """Callback for when a message is published."""
        # logger.debug(f"Message {mid} published.")
        pass # Usually not needed to log every publish

    def connect(self):
        """Connects to the MQTT broker."""
        try:
            logger.info(f"Attempting to connect to MQTT broker: {self.broker_address}:{self.broker_port}")
            self.client.connect(self.broker_address, self.broker_port, 60)
            self.client.loop_start() # Start background thread for network traffic
        except Exception as e:
            logger.error(f"Error connecting to MQTT broker: {e}")

    def disconnect(self):
        """Disconnects from the MQTT broker."""
        logger.info("Disconnecting from MQTT broker...")
        self.client.loop_stop() # Stop background thread
        self.client.disconnect()

    def publish(self, payload):
        """Publishes a message to the configured MQTT topic."""
        if self.client.is_connected():
            result = self.client.publish(self.topic, payload)
            if result.rc == paho_mqtt.MQTT_ERR_SUCCESS:
                logger.debug(f"Published message to topic '{self.topic}': {payload}")
            else:
                logger.warning(f"Failed to publish message to topic '{self.topic}'. RC: {result.rc}")
        else:
            logger.warning("MQTT client not connected. Cannot publish message.")

```

**3. Create `timer.py`**

This file will contain the core logic for the stopwatch and timer. We'll start with a basic structure.

```
timer.py
<<<<<<< SEARCH
