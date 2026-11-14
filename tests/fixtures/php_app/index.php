<?php
require_once __DIR__ . '/inc/db.php';
require_once __DIR__ . '/src/User.php';

$id = isset($_GET['id']) ? (int) $_GET['id'] : 0;
$user = User::find($id);

if (!$user) {
    header('HTTP/1.0 404 Not Found');
    echo 'User not found';
    exit;
}

echo htmlspecialchars($user['email']);
