<?php

class User
{
    public static function find(int $id): ?array
    {
        $pdo = db_connect();
        $stmt = $pdo->prepare('SELECT id, email, role FROM users WHERE id = :id');
        $stmt->execute([':id' => $id]);
        $row = $stmt->fetch(PDO::FETCH_ASSOC);
        return $row ?: null;
    }

    public static function promote(int $id): void
    {
        $pdo = db_connect();
        $stmt = $pdo->prepare('UPDATE users SET role = :role WHERE id = :id');
        $stmt->execute([':role' => 'admin', ':id' => $id]);
    }
}
