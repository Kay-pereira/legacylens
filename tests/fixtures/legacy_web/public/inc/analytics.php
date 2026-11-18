<?php

function campaign_source(): string
{
    return $_GET['campaign'] ?? 'direct';
}
