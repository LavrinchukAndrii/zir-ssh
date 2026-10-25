<?php
// A page that touches the database, so web load shows up as DB queries too.
$db = new mysqli('localhost', 'app', 'app', 'app');
$r = $db->query('SELECT COUNT(*) AS n FROM visits');
$db->query('INSERT INTO visits (path) VALUES ("' . $db->real_escape_string($_SERVER['REQUEST_URI']) . '")');
if (isset($_GET['slow'])) { $db->query('SELECT SLEEP(2)'); }
echo 'ok ', $r->fetch_assoc()['n'], "\n";
