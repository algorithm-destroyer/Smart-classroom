# 🎓 Smart Classroom Management System

A web-based **Smart Classroom Management System** designed to simplify and digitize classroom activities by providing a centralized platform for students and teachers.

The system offers features such as student and teacher authentication, attendance management, assignment management, study materials, recorded classes, notices, and academic information.

## 🚀 Features

### 👨‍🎓 Student Module
- Student registration and login.
- Personalized student dashboard.
- Student profile management.
- Attendance tracking.
- Assignment access and management.
- Access to recorded classes.
- Study notes and learning materials.
- Classroom notices and announcements.
- Academic profile and information.
- Forgot password functionality.

### 👩‍🏫 Teacher Module
- Dedicated teacher registration and login.
- Teacher dashboard.
- Teacher profile management.
- Student attendance management.
- Assignment management.
- Study material management.
- Recorded class management.
- Notice and announcement management.
- Student queries and communication.
- Academic analytics dashboard.

### 📊 Additional Functionality
- Firebase integration for database management.
- Face recognition-related functionality.
- Session-based authentication.
- Excel report generation.
- Separate access for students and teachers.

## 🛠️ Technologies Used

| Technology | Purpose |
|---|---|
| Python | Backend development |
| Flask | Web application framework |
| Firebase Authentication | User authentication |
| Firebase Firestore | Database management |
| HTML | Web page structure |
| CSS | Styling and UI design |
| JavaScript | Frontend interactivity |
| OpenCV | Image processing |
| Face Recognition | Facial recognition functionality |
| NumPy | Numerical operations |
| OpenPyXL | Excel report generation |

## 📁 Project Structure

```text
Smart-classroom/
│
├── app.py
│
├── templates/
│   ├── intro.html
│   ├── login.html
│   ├── register.html
│   ├── teacher_login.html
│   ├── teacher_register.html
│   ├── home.html
│   ├── attendance.html
│   ├── assignments.html
│   ├── notes.html
│   ├── notices.html
│   └── ... 
│
├── temp/
│
├── serviceAccountKey.json
│
└── README.md
```

## ⚙️ Installation and Setup

Follow these steps to run the project locally.

### 1. Clone the Repository

```bash
git clone https://github.com/algorithm-destroyer/Smart-classroom.git
```

### 2. Navigate to the Project Directory

```bash
cd Smart-classroom
```

### 3. Create a Virtual Environment (Recommended)

```bash
python -m venv venv
```

Activate the environment:

**Windows:**
```bash
venv\Scripts\activate
```

**Mac/Linux:**
```bash
source venv/bin/activate
```

### 4. Install Dependencies

```bash
pip install Flask firebase-admin requests opencv-python face-recognition numpy openpyxl
```

### 5. Configure Firebase

- Create a Firebase project.
- Enable Firebase Authentication and Cloud Firestore.
- Generate a Firebase service account key.
- Configure the application to access your Firebase project.
- Keep your Firebase credentials secure and never expose them publicly.

### 6. Run the Application

```bash
python app.py
```

Open your browser and visit:

```text
http://127.0.0.1:5000/
```

## 🎯 Project Objective

The primary objective of Smart Classroom is to create a digital learning environment that improves communication and simplifies classroom management.

By bringing students and teachers onto a single platform, the system aims to reduce manual administrative work, improve accessibility to academic resources, and make classroom activities more organized and efficient.

## 🔮 Future Enhancements

- Real-time attendance notifications.
- Advanced student performance analytics.
- Online assignment submission and evaluation.
- Improved facial recognition-based attendance.
- Live virtual classroom integration.
- Mobile application support.

## 👩‍💻 Contributors

Developed as a collaborative project to explore web development, database integration, authentication, and smart classroom management solutions.

---

⭐ If you find this project interesting, feel free to explore the repository and contribute!
