CREATE TABLE IF NOT EXISTS users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    email VARCHAR(100) NOT NULL UNIQUE,
    password VARCHAR(255) NOT NULL
);

CREATE TABLE IF NOT EXISTS transcriptions (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT,
    user_email VARCHAR(100),
    job_id VARCHAR(64),
    original_filename VARCHAR(255),
    audio_file VARCHAR(255),
    transcript_file VARCHAR(255),
    summary_file VARCHAR(255),
    json_file VARCHAR(255),
    combined_file VARCHAR(255),
    srt_file VARCHAR(255),
    detected_language VARCHAR(80),
    duration_seconds FLOAT DEFAULT 0,
    word_count INT DEFAULT 0,
    processing_seconds FLOAT DEFAULT 0,
    action_items_count INT DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
