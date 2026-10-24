#!/bin/sh
# /etc/letsencrypt/renewal-hooks/deploy/nbe-mosquitto.sh
# mosquitto can't read /etc/letsencrypt; give it a copy of the nbe.evm100.org cert on every renewal.
set -e
D=nbe.evm100.org
case " ${RENEWED_DOMAINS:-$D} " in *" $D "*) ;; *) exit 0 ;; esac
install -d -o root -g mosquitto -m 750 /etc/mosquitto/certs/$D
install -o root -g mosquitto -m 640 /etc/letsencrypt/live/$D/fullchain.pem /etc/mosquitto/certs/$D/fullchain.pem
install -o root -g mosquitto -m 640 /etc/letsencrypt/live/$D/privkey.pem   /etc/mosquitto/certs/$D/privkey.pem
systemctl restart mosquitto || true
