#!/bin/sh
set -eu

cert_dir=/etc/dovecot/certs
mkdir -p "$cert_dir"

if [ ! -s "$cert_dir/dovecot.crt" ] || [ ! -s "$cert_dir/dovecot.key" ]; then
    openssl req -x509 -nodes -newkey rsa:2048 \
        -keyout "$cert_dir/dovecot.key" \
        -out "$cert_dir/dovecot.crt" \
        -days 1 \
        -subj "/CN=dovecot"
    chmod 600 "$cert_dir/dovecot.key"
fi

mail_root=/var/mail/vmail/testuser/Maildir
mkdir -p "$mail_root/cur" "$mail_root/new" "$mail_root/tmp"
chown -R vmail:vmail /var/mail/vmail

exec dovecot -F -c /etc/dovecot/dovecot.conf