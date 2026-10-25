# Test server for zir: nginx + PHP-FPM + MariaDB + sshd, like a typical small VPS.
#   docker build -t zir-testhost tests/docker
#   docker run -d --name zir-testhost -p 2222:22 -v "$PWD/key.pub:/root/.ssh/authorized_keys:ro" zir-testhost
FROM debian:12-slim
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        openssh-server nginx php8.2-fpm php8.2-mysql mariadb-server procps iproute2 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /run/sshd /run/php /root/.ssh && chmod 700 /root/.ssh \
    && sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config \
    && sed -i 's/^pm.max_children = .*/pm.max_children = 4/' /etc/php/8.2/fpm/pool.d/www.conf
COPY site.conf /etc/nginx/sites-available/default
COPY index.php /var/www/html/index.php
COPY start.sh /start.sh
RUN chmod +x /start.sh
EXPOSE 22 80
CMD ["/start.sh"]
