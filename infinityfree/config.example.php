<?php
declare(strict_types=1);
// Copy to config.php on your host and enter the credentials from InfinityFree.
// Never commit config.php or share the database password.
const DB_HOST = 'YOUR_MYSQL_HOST';
const DB_NAME = 'YOUR_FULL_DATABASE_NAME';
const DB_USER = 'YOUR_MYSQL_USERNAME';
const DB_PASS = 'YOUR_MYSQL_PASSWORD';
function db(): PDO {
 static $pdo = null;
 if ($pdo instanceof PDO) return $pdo;
 $pdo = new PDO('mysql:host='.DB_HOST.';dbname='.DB_NAME.';charset=utf8mb4', DB_USER, DB_PASS, [
  PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
  PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
  PDO::ATTR_EMULATE_PREPARES => false,
 ]);
 return $pdo;
}
