<?php

function current_user_id(): int
{
    return (int) ($_COOKIE['uid'] ?? 0);
}

function search(string $query, int $page): array
{
    $term = $_POST['term'] ?? $query;
    $pdo = new PDO('mysql:host=localhost;dbname=legacy_app', 'legacy', 'legacy');
    $stmt = $pdo->prepare(
        'SELECT id, title FROM articles WHERE title LIKE :term ORDER BY id LIMIT 20 OFFSET :offset'
    );
    $stmt->execute([':term' => '%' . $term . '%', ':offset' => ($page - 1) * 20]);
    return $stmt->fetchAll(PDO::FETCH_ASSOC);
}

function render_results(array $rows, int $viewer): string
{
    return json_encode(['viewer' => $viewer, 'rows' => $rows]);
}
