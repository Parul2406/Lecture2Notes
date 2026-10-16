-- Lecture to Notes: database schema (PostgreSQL)
-- Generated from app/models.py. To use Postgres, set e.g.
--   DATABASE_URL=postgresql+psycopg://user:pass@localhost/lecture_notes
-- (and `pip install psycopg[binary]`). The app creates these tables on startup,
-- or run this file by hand:  psql -d lecture_notes -f db/schema.postgres.sql

-- Teachers and students
CREATE TABLE users (
	id SERIAL NOT NULL,
	name VARCHAR(120) NOT NULL,
	email VARCHAR(255) NOT NULL,             -- stored lower-cased
	password_hash VARCHAR(255) NOT NULL,     -- bcrypt
	role VARCHAR(20) NOT NULL,               -- 'teacher' | 'student'
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
);
CREATE UNIQUE INDEX ix_users_email ON users (email);

-- Uploaded lectures and their processing state
CREATE TABLE lectures (
	id SERIAL NOT NULL,
	teacher_id INTEGER NOT NULL,
	title VARCHAR(200) NOT NULL,
	description TEXT NOT NULL,
	media_type VARCHAR(10) NOT NULL,         -- 'video' | 'audio'
	original_filename VARCHAR(255) NOT NULL,
	media_path VARCHAR(500) NOT NULL,
	audio_path VARCHAR(500),
	duration FLOAT,                          -- seconds
	status VARCHAR(20) NOT NULL,             -- 'uploaded' | 'extracting_audio' | 'transcribing' | 'completed' | 'failed'
	progress FLOAT NOT NULL,                 -- 0..1 during transcription
	error TEXT,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(teacher_id) REFERENCES users (id) ON DELETE CASCADE
);
CREATE INDEX ix_lectures_status ON lectures (status);
CREATE INDEX ix_lectures_teacher_id ON lectures (teacher_id);

-- Whisper output, one row per lecture
CREATE TABLE transcripts (
	id SERIAL NOT NULL,
	lecture_id INTEGER NOT NULL,
	model VARCHAR(100) NOT NULL,
	language VARCHAR(10),
	language_probability FLOAT,
	duration FLOAT,
	text TEXT NOT NULL,
	-- [{"id", "start", "end", "text", "avg_logprob", "no_speech_prob",
	--   "words": [{"start", "end", "word", "probability"}]}]
	segments JSON NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (lecture_id),
	FOREIGN KEY(lecture_id) REFERENCES lectures (id) ON DELETE CASCADE
);
