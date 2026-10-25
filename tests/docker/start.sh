#!/bin/sh
set -e
service mariadb start
mysql -e "CREATE DATABASE IF NOT EXISTS app; CREATE USER IF NOT EXISTS 'app'@'localhost' IDENTIFIED BY 'app'; GRANT ALL ON app.* TO 'app'@'localhost'; CREATE TABLE IF NOT EXISTS app.visits (id INT AUTO_INCREMENT PRIMARY KEY, path VARCHAR(255), at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);"
service php8.2-fpm start
service nginx start
exec /usr/sbin/sshd -D -e
