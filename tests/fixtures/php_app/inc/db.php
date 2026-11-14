<?php

function db_connect(): PDO
{
    $dsn = 'mysql:host=localhost;dbname=legacy_app';
    return new PDO($dsn, 'legacy', 'legacy');
}
