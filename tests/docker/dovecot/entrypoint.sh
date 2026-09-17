#!/bin/sh
set -eu

cert_dir=/etc/dovecot/certs
mkdir -p "$cert_dir"

issue_self_signed_cert() {
    if [ ! -s "$cert_dir/dovecot.crt" ] || [ ! -s "$cert_dir/dovecot.key" ]; then
        openssl req -x509 -nodes -newkey rsa:2048 \
            -keyout "$cert_dir/dovecot.key" \
            -out "$cert_dir/dovecot.crt" \
            -days 1 \
            -subj "/CN=dovecot"
        chmod 600 "$cert_dir/dovecot.key"
    fi
}

# Issues a server certificate signed by a throwaway test CA (instead of a
# self-signed leaf certificate) and exports only the CA certificate to a
# bind-mounted directory so host-side tests can use it as ca_cert_path
# without disabling certificate verification. The CA private key never
# leaves this container.
issue_ca_signed_cert() {
    ca_dir=/etc/dovecot/ca
    export_dir=/etc/dovecot/ca-export
    mkdir -p "$ca_dir" "$export_dir"

    if [ ! -s "$ca_dir/mail-dock-ca.crt" ] || [ ! -s "$ca_dir/mail-dock-ca.key" ]; then
        # "openssl req -x509" already applies the default v3_ca extensions
        # (subjectKeyIdentifier, authorityKeyIdentifier, critical
        # basicConstraints CA:true) from openssl.cnf, so only keyUsage is
        # added explicitly here; modern OpenSSL rejects a trust-anchor
        # missing keyCertSign when validating the chain (adding the other
        # three again via -addext would duplicate them instead).
        openssl req -x509 -nodes -newkey rsa:2048 \
            -keyout "$ca_dir/mail-dock-ca.key" \
            -out "$ca_dir/mail-dock-ca.crt" \
            -days 1 \
            -subj "/CN=mail-dock-test-ca" \
            -addext "keyUsage=critical,keyCertSign,cRLSign"
        chmod 600 "$ca_dir/mail-dock-ca.key"
    fi

    if [ ! -s "$cert_dir/dovecot.crt" ] || [ ! -s "$cert_dir/dovecot.key" ]; then
        cat > "$ca_dir/dovecot.ext" <<'EOF'
basicConstraints=CA:FALSE
subjectAltName=IP:127.0.0.1,DNS:localhost
authorityKeyIdentifier=keyid,issuer
extendedKeyUsage=serverAuth
EOF
        openssl req -nodes -newkey rsa:2048 \
            -keyout "$cert_dir/dovecot.key" \
            -out "$ca_dir/dovecot.csr" \
            -subj "/CN=dovecot-ca-signed"
        openssl x509 -req \
            -in "$ca_dir/dovecot.csr" \
            -CA "$ca_dir/mail-dock-ca.crt" \
            -CAkey "$ca_dir/mail-dock-ca.key" \
            -CAcreateserial \
            -out "$cert_dir/dovecot.crt" \
            -days 1 \
            -extfile "$ca_dir/dovecot.ext"
        chmod 600 "$cert_dir/dovecot.key"
    fi

    cp "$ca_dir/mail-dock-ca.crt" "$export_dir/mail-dock-ca.crt"
}

if [ "${MAILDOCK_ISSUE_CA_CERT:-0}" = "1" ]; then
    issue_ca_signed_cert
else
    issue_self_signed_cert
fi

mail_root=/var/mail/vmail/testuser/Maildir
mkdir -p "$mail_root/cur" "$mail_root/new" "$mail_root/tmp"
chown -R vmail:vmail /var/mail/vmail

exec dovecot -F -c /etc/dovecot/dovecot.conf