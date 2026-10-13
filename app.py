
import streamlit as st

st.set_page_config(
    page_title="Lecture2Notes",
    page_icon="📚",
    layout="wide"
)

st.title("📚 Lecture2Notes")
st.subheader("Turn your lectures into exam-ready study material")

st.write(
    "Upload an audio or video lecture to prepare "
    "transcripts, structured notes, definitions and MCQs."
)

st.divider()

st.header("Upload Your Lecture")

uploaded_file = st.file_uploader(
    "Choose an audio or video file",
    type=[
        "mp3", "wav", "m4a",
        "mp4", "mov", "avi", "mkv"
    ],
    help="Supported formats: MP3, WAV, M4A, MP4, MOV, AVI and MKV."
)

if uploaded_file is not None:
    st.success("Lecture uploaded successfully!")

    file_name = uploaded_file.name
    st.write("**File name:**", file_name)

    file_extension = file_name.rsplit(".", 1)[-1].lower()

    if file_extension in ["mp4", "mov", "avi", "mkv"]:
        st.video(uploaded_file)
    else:
        st.audio(uploaded_file)

    if st.button("Generate Study Material"):
        st.info(
            "Your lecture is ready for processing. "
            "AI transcription and note generation "
            "will be added in the next development stage."
        )

st.divider()

st.header("Your Study Material")

tab1, tab2, tab3, tab4 = st.tabs(
    ["Transcript", "Notes", "Definitions", "MCQs"]
)

with tab1:
    st.write("Your lecture transcript will appear here.")

with tab2:
    st.write("Your organized study notes will appear here.")

with tab3:
    st.write("Important definitions will appear here.")

with tab4:
    st.write("Practice questions will appear here.")
