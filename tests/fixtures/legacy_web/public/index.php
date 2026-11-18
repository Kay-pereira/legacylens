<?php
require_once 'inc/helpers.php';

$query = $_GET['q'] ?? '';
$page = (int) ($_GET['page'] ?? 1);
$viewer = current_user_id();

echo render_results(search($query, $page), $viewer);
