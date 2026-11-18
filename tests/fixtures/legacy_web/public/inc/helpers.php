<?php

function current_user_id(): int
{
    return (int) ($_COOKIE['uid'] ?? 0);
}

function search(string $query, int $page): array
{
    $term = $_POST['term'] ?? $query;
    return ['term' => $term, 'page' => $page];
}

function render_results(array $rows, int $viewer): string
{
    return json_encode(['viewer' => $viewer, 'rows' => $rows]);
}
