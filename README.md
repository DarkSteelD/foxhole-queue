# Foxhole Queue Bot

A simple Discord bot that displays Foxhole map activity (casualties) as a proxy for queues.

## Features

- `!queues` (or `!status`, `!activity`): Shows the top 10 maps with the highest casualties in the current war. High casualties usually indicate large battles and potential queues.
- `!map <name>`: Shows detailed casualty and enlistment statistics for a specific map.

## Setup

1.  **Install Dependencies**:
    ```bash
    pip install -r requirements.txt
    ```

2.  **Configure Token**:
    - Open `.env` file.
    - Replace `your_token_here` with your actual Discord Bot Token.
    - Example: `DISCORD_TOKEN=MTAy...`

3.  **Run the Bot**:
    ```bash
    python3 main.py
    ```

## Note

Official queue data API was removed by the developers. This bot uses **casualty reports** from the official War API as an indicator of activity.
