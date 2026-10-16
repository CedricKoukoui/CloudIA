"""Test de la logique TLS du client (v5), sans RabbitMQ ni cluster.

Génère une autorité de certification, un certificat serveur et un certificat client avec openssl,
démarre un petit serveur TLS qui EXIGE un certificat client, puis vérifie que :
  - amqp://  : aucune option TLS n'est posée ;
  - amqps:// sans AMQP_CA_FILE : refus explicite ;
  - amqps:// avec CA + certificat client : la poignée de main réussit ;
  - amqps:// avec CA mais sans certificat client : le serveur refuse ;
  - amqps:// avec une autre CA : le certificat du serveur est rejeté.
"""
import os
import socket
import ssl
import subprocess
import tempfile
import threading

from amqp_tls import build_params


def run(*cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


def make_pki(d):
    for name in ("ca", "other-ca"):
        open(os.path.join(d, name + ".cnf"), "w").write(
            "[req]\ndistinguished_name=dn\nx509_extensions=v3\nprompt=no\n[dn]\nCN=%s\n"
            "[v3]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n"
            "subjectKeyIdentifier=hash\n" % name)
        run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", name + ".key",
            "-out", name + ".crt", "-days", "2", "-config", name + ".cnf", cwd=d)
    for name, usage, san in (("server", "serverAuth", "subjectAltName=DNS:localhost\n"),
                             ("client", "clientAuth", "")):
        open(os.path.join(d, name + ".ext"), "w").write(
            "basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=%s\nauthorityKeyIdentifier=keyid\n%s" % (usage, san))
        run("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", name + ".key", "-out", name + ".csr",
            "-subj", "/CN=" + name, cwd=d)
        run("openssl", "x509", "-req", "-in", name + ".csr", "-CA", "ca.crt", "-CAkey", "ca.key",
            "-CAcreateserial", "-out", name + ".crt", "-days", "2", "-extfile", name + ".ext", cwd=d)


def serve(d, listener, stop):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(os.path.join(d, "server.crt"), os.path.join(d, "server.key"))
    ctx.load_verify_locations(os.path.join(d, "ca.crt"))
    ctx.verify_mode = ssl.CERT_REQUIRED
    while not stop.is_set():
        try:
            raw, _ = listener.accept()
        except OSError:
            return
        try:
            with ctx.wrap_socket(raw, server_side=True) as conn:
                conn.sendall(b"OK")
        except (ssl.SSLError, OSError):
            pass


def handshake(params, port):
    """Tente la connexion avec les options TLS de pika ; renvoie True si le serveur répond."""
    ctx = params.ssl_options.context
    try:
        with socket.create_connection(("localhost", port), timeout=5) as raw:
            with ctx.wrap_socket(raw, server_hostname="localhost") as conn:
                return conn.recv(2) == b"OK"
    except (ssl.SSLError, OSError):
        return False


def main():
    with tempfile.TemporaryDirectory() as d:
        make_pki(d)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        port = listener.getsockname()[1]
        stop = threading.Event()
        threading.Thread(target=serve, args=(d, listener, stop), daemon=True).start()
        url = "amqps://u:p@localhost:%d/%%2F" % port
        env = os.environ
        for k in ("AMQP_CA_FILE", "AMQP_CERT_FILE", "AMQP_KEY_FILE"):
            env.pop(k, None)

        assert build_params("amqp://u:p@localhost:5672/%2F").ssl_options is None
        print("OK  amqp:// : pas de TLS")

        try:
            build_params(url)
            raise SystemExit("ECHEC amqps:// sans CA aurait dû être refusé")
        except RuntimeError:
            print("OK  amqps:// sans AMQP_CA_FILE : refusé")

        env["AMQP_CA_FILE"] = os.path.join(d, "ca.crt")
        env["AMQP_CERT_FILE"] = os.path.join(d, "client.crt")
        env["AMQP_KEY_FILE"] = os.path.join(d, "client.key")
        assert handshake(build_params(url), port), "poignée de main TLS mutuelle refusée"
        print("OK  CA + certificat client : poignée de main réussie")

        del env["AMQP_CERT_FILE"], env["AMQP_KEY_FILE"]
        assert not handshake(build_params(url), port), "le serveur a accepté un client sans certificat"
        print("OK  sans certificat client : refusé par le serveur")

        env["AMQP_CA_FILE"] = os.path.join(d, "other-ca.crt")
        env["AMQP_CERT_FILE"] = os.path.join(d, "client.crt")
        env["AMQP_KEY_FILE"] = os.path.join(d, "client.key")
        assert not handshake(build_params(url), port), "un serveur signé par une autre CA a été accepté"
        print("OK  autre autorité de certification : certificat du serveur rejeté")
        stop.set()
        listener.close()


if __name__ == "__main__":
    main()
