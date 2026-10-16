"""Paramètres de connexion RabbitMQ, avec TLS mutuel optionnel (v5).

- URL en amqp://  : connexion en clair (sonde Docker Compose de la CI, développement local).
- URL en amqps:// : connexion chiffrée. Le certificat du broker est vérifié avec l'autorité de
  certification AMQP_CA_FILE ; si AMQP_CERT_FILE et AMQP_KEY_FILE sont fournis, le client
  présente aussi son propre certificat (authentification mutuelle).
"""
import os
import ssl

import pika


def build_params(url):
    params = pika.URLParameters(url)
    if url.startswith("amqps://"):
        ca_file = os.environ.get("AMQP_CA_FILE")
        if not ca_file:
            raise RuntimeError("URL amqps:// sans AMQP_CA_FILE : le certificat du broker ne serait pas vérifié.")
        context = ssl.create_default_context(cafile=ca_file)
        cert_file = os.environ.get("AMQP_CERT_FILE")
        key_file = os.environ.get("AMQP_KEY_FILE")
        if cert_file and key_file:
            context.load_cert_chain(cert_file, key_file)
        params.ssl_options = pika.SSLOptions(context)
    return params
