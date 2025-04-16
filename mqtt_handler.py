import os
import paho.mqtt.client as paho_mqtt
import logging

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

from typing import Callable, Optional

class MQTTHandler:
    """Handles MQTT connection and publishing."""

    def __init__(self, broker_address: str, broker_port: int, topic: str, username: Optional[str] = None, password: Optional[str] = None):
        """
        Initializes the MQTT client.

        Args:
            broker_address: The MQTT broker address.
            broker_port: The MQTT broker port.
            topic: The MQTT topic to publish to.
            username: Optional MQTT username.
            password: Optional MQTT password.
        """
        self.broker_address = broker_address
        self.broker_port = int(broker_port)
        self.topic = topic
        self.username = username
        self.password = password
        # self._on_connect_callback = on_connect_callback # Removed: Blanking is no longer triggered on connect
        self.client = paho_mqtt.Client() # Removed CallbackAPIVersion argument for paho-mqtt v1.x compatibility
        self._message_callbacks = {} # topic -> callback function
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_publish = self._on_publish
        self.client.on_message = self._on_message # Add message callback handler

        if self.username and self.password:
            self.client.username_pw_set(self.username, self.password)

    def _on_connect(self, client, userdata, flags, rc):
        """Callback for when the client connects to the MQTT broker."""
        if rc == 0:
            logger.info(f"Successfully connected to MQTT Broker at {self.broker_address}:{self.broker_port}")
            # Resubscribe to topics upon reconnection
            for topic in self._message_callbacks:
                self._subscribe(topic)
            # Removed: Blanking is no longer triggered on connect
            # if self._on_connect_callback:
            #     try:
            #         self._on_connect_callback()
            #     except Exception as e:
            #         logger.error(f"Error executing on_connect_callback: {e}")
        else:
            logger.error(f"Failed to connect to MQTT Broker, return code {rc}")

    def _on_disconnect(self, client, userdata, rc):
        """Callback for when the client disconnects from the MQTT broker."""
        logger.info("Disconnected from MQTT Broker.")
        if rc != 0:
            logger.warning(f"Unexpected MQTT disconnection. Will auto-reconnect: {rc}")

    def _on_message(self, client, userdata, msg):
        """Callback for when a message is received on a subscribed topic."""
        topic = msg.topic
        payload = msg.payload.decode("utf-8")
        logger.debug(f"Received message on topic '{topic}': {payload}")
        if topic in self._message_callbacks:
            try:
                self._message_callbacks[topic](topic, payload)
            except Exception as e:
                logger.error(f"Error executing message callback for topic {topic}: {e}")
        else:
            logger.warning(f"Received message on unsubscribed or unhandled topic: {topic}")


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

    def _subscribe(self, topic: str):
        """Internal helper to subscribe to a topic."""
        if self.client.is_connected():
            logger.info(f"Subscribing to topic: {topic}")
            result, mid = self.client.subscribe(topic)
            if result == paho_mqtt.MQTT_ERR_SUCCESS:
                logger.debug(f"Successfully subscribed to {topic} (MID: {mid})")
            else:
                logger.error(f"Failed to subscribe to {topic}. Result code: {result}")
        else:
            logger.warning(f"Cannot subscribe to {topic}: MQTT client not connected.")


    def subscribe(self, topic: str, callback: Callable[[str, str], None]):
        """
        Subscribes to an MQTT topic and registers a callback for messages.

        Args:
            topic: The MQTT topic to subscribe to.
            callback: A function to call when a message arrives on the topic.
                      Signature: callback(topic: str, payload: str)
        """
        if not callable(callback):
             logger.error(f"Cannot subscribe to {topic}: Provided callback is not callable.")
             return

        self._message_callbacks[topic] = callback
        self._subscribe(topic) # Attempt subscription immediately if connected
