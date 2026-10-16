-- Lecture to Notes: database schema (SQLite)
-- Generated from app/models.py. The app creates these tables automatically on startup;
-- this file is for reference or for creating the DB by hand:
--   sqlite3 data/app.db < db/schema.sql

PRAGMA foreign_keys = ON;

-- Teachers and students
CREATE TABLE users (
	id INTEGER NOT NULL,
	name VARCHAR(120) NOT NULL,
	email VARCHAR(255) NOT NULL,             -- stored lower-cased
	password_hash VARCHAR(255) NOT NULL,     -- bcrypt
	role VARCHAR(20) NOT NULL,               -- 'teacher' | 'student'
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);
CREATE UNIQUE INDEX ix_users_email ON users (email);

-- Uploaded lectures and their processing state
CREATE TABLE lectures (
	id INTEGER NOT NULL,
	teacher_id INTEGER NOT NULL,
	title VARCHAR(200) NOT NULL,
	description TEXT NOT NULL,
	media_type VARCHAR(10) NOT NULL,         -- 'video' | 'audio'
	original_filename VARCHAR(255) NOT NULL,
	media_path VARCHAR(500) NOT NULL,        -- data/uploads/<uuid>.<ext>
	audio_path VARCHAR(500),                 -- data/audio/lecture_<id>.wav (16 kHz mono, from ffmpeg)
	duration FLOAT,                          -- seconds
	status VARCHAR(20) NOT NULL,             -- 'uploaded' | 'extracting_audio' | 'transcribing' | 'completed' | 'failed'
	progress FLOAT NOT NULL,                 -- 0..1 during transcription
	error TEXT,                              -- set when status = 'failed'
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(teacher_id) REFERENCES users (id) ON DELETE CASCADE
);
CREATE INDEX ix_lectures_status ON lectures (status);
CREATE INDEX ix_lectures_teacher_id ON lectures (teacher_id);

-- Whisper output, one row per lecture
CREATE TABLE transcripts (
	id INTEGER NOT NULL,
	lecture_id INTEGER NOT NULL,
	model VARCHAR(100) NOT NULL,             -- whisper model name, e.g. 'small'
	language VARCHAR(10),
	language_probability FLOAT,
	duration FLOAT,
	text TEXT NOT NULL,                      -- full transcript as plain text
	-- JSON array of segments:
	-- [{"id", "start", "end", "text", "avg_logprob", "no_speech_prob",
	--   "words": [{"start", "end", "word", "probability"}]}]
	segments JSON NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (lecture_id),
	FOREIGN KEY(lecture_id) REFERENCES lectures (id) ON DELETE CASCADE
);
