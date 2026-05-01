from flask import Flask, render_template, request, redirect, url_for, flash
import pymysql
import pymysql.cursors
from datetime import date
from decimal import Decimal

app = Flask(__name__)
app.secret_key = 'trainpro-secret-2026'

DB_CONFIG = {
    'host': 'localhost',
    'user': 'root',
    'password': 'Vithu1119@',
    'db': 'trainpro',
    'charset': 'utf8mb4',
    'cursorclass': pymysql.cursors.DictCursor
}

def get_db():
    return pymysql.connect(**DB_CONFIG)


def calculate_fee(course_id, company_id=None):
    """Tiered fee: £5000 first delegate, £2500 second, £1500 thereafter."""
    TIERS = [Decimal('5000.00'), Decimal('2500.00'), Decimal('1500.00')]
    if not company_id:
        return TIERS[0]
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM registrations WHERE course_id=%s AND company_id=%s",
                (course_id, company_id)
            )
            count = cur.fetchone()['cnt']
    finally:
        conn.close()
    if count == 0:
        return TIERS[0]
    elif count == 1:
        return TIERS[1]
    else:
        return TIERS[2]


# ─────────────────────────────────────────
# Public routes
# ─────────────────────────────────────────

@app.route('/')
def index():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.course_id, c.course_name, c.description, c.max_delegates,
                       c.base_fee, c.is_outsourced,
                       cd.delivery_date, v.venue_name,
                       COUNT(r.registration_id) AS enrolled
                FROM courses c
                LEFT JOIN course_deliveries cd ON c.course_id = cd.course_id
                LEFT JOIN venues v ON cd.venue_id = v.venue_id
                LEFT JOIN registrations r ON c.course_id = r.course_id
                GROUP BY c.course_id, cd.delivery_id
                ORDER BY cd.delivery_date
            """)
            courses = cur.fetchall()
    finally:
        conn.close()
    return render_template('index.html', courses=courses)


@app.route('/course/<int:course_id>')
def course_detail(course_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.*, cd.delivery_date, v.venue_name, v.address,
                       ep.provider_name,
                       COUNT(r.registration_id) AS enrolled
                FROM courses c
                LEFT JOIN course_deliveries cd ON c.course_id = cd.course_id
                LEFT JOIN venues v ON cd.venue_id = v.venue_id
                LEFT JOIN external_providers ep ON c.provider_id = ep.provider_id
                LEFT JOIN registrations r ON c.course_id = r.course_id
                WHERE c.course_id = %s
                GROUP BY c.course_id, cd.delivery_id
            """, (course_id,))
            course = cur.fetchone()
            if not course:
                flash('Course not found.', 'danger')
                return redirect(url_for('index'))

            cur.execute("""
                SELECT s.name, s.role FROM staff s
                JOIN delivery_staff ds ON s.staff_id = ds.staff_id
                JOIN course_deliveries cd ON ds.delivery_id = cd.delivery_id
                WHERE cd.course_id = %s
            """, (course_id,))
            trainers = cur.fetchall()

            cur.execute("SELECT * FROM companies ORDER BY company_name")
            companies = cur.fetchall()
    finally:
        conn.close()
    return render_template('course_detail.html', course=course, trainers=trainers, companies=companies)


@app.route('/register/<int:course_id>', methods=['GET', 'POST'])
def register(course_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM courses WHERE course_id=%s", (course_id,))
            course = cur.fetchone()
            if not course:
                flash('Course not found.', 'danger')
                return redirect(url_for('index'))

            cur.execute("SELECT * FROM companies ORDER BY company_name")
            companies = cur.fetchall()

        if request.method == 'POST':
            name = request.form['name'].strip()
            email = request.form['email'].strip()
            phone = request.form.get('phone', '').strip()
            company_id = request.form.get('company_id') or None
            reg_by = request.form.get('registered_by', '').strip() or None

            if not name or not email:
                flash('Name and email are required.', 'danger')
                return render_template('register.html', course=course, companies=companies)

            with conn.cursor() as cur:
                # Check course capacity
                cur.execute("""
                    SELECT COUNT(*) AS cnt FROM registrations WHERE course_id=%s
                """, (course_id,))
                enrolled = cur.fetchone()['cnt']
                if enrolled >= course['max_delegates']:
                    flash('This course is fully booked.', 'danger')
                    return redirect(url_for('course_detail', course_id=course_id))

                # Upsert delegate
                cur.execute("SELECT delegate_id FROM delegates WHERE email=%s", (email,))
                existing = cur.fetchone()
                if existing:
                    delegate_id = existing['delegate_id']
                else:
                    cur.execute(
                        "INSERT INTO delegates (name, email, phone) VALUES (%s,%s,%s)",
                        (name, email, phone or None)
                    )
                    delegate_id = cur.lastrowid

                # Check for duplicate registration
                cur.execute(
                    "SELECT registration_id FROM registrations WHERE delegate_id=%s AND course_id=%s",
                    (delegate_id, course_id)
                )
                if cur.fetchone():
                    flash('This delegate is already registered for this course.', 'warning')
                    conn.rollback()
                    return render_template('register.html', course=course, companies=companies)

                fee = calculate_fee(course_id, company_id)
                cur.execute("""
                    INSERT INTO registrations
                        (delegate_id, course_id, company_id, registration_date, registered_by_employee, fee_paid)
                    VALUES (%s,%s,%s,%s,%s,%s)
                """, (delegate_id, course_id, company_id, date.today(), reg_by, fee))
                reg_id = cur.lastrowid

                payee_type = 'Company' if company_id else 'Individual'
                cur.execute("""
                    INSERT INTO invoices (registration_id, amount, invoice_date, payee_type)
                    VALUES (%s,%s,%s,%s)
                """, (reg_id, fee, date.today(), payee_type))

            conn.commit()
            flash('Registration successful!', 'success')
            return redirect(url_for('invoice', reg_id=reg_id))
    finally:
        conn.close()

    return render_template('register.html', course=course, companies=companies)


@app.route('/invoice/<int:reg_id>')
def invoice(reg_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT i.*, r.registration_date, r.registered_by_employee, r.fee_paid,
                       d.name AS delegate_name, d.email AS delegate_email, d.phone,
                       c.course_name, c.description,
                       co.company_name, co.address AS company_address,
                       cd.delivery_date, v.venue_name
                FROM invoices i
                JOIN registrations r ON i.registration_id = r.registration_id
                JOIN delegates d ON r.delegate_id = d.delegate_id
                JOIN courses c ON r.course_id = c.course_id
                LEFT JOIN companies co ON r.company_id = co.company_id
                LEFT JOIN course_deliveries cd ON c.course_id = cd.course_id
                LEFT JOIN venues v ON cd.venue_id = v.venue_id
                WHERE i.registration_id = %s
            """, (reg_id,))
            inv = cur.fetchone()
            if not inv:
                flash('Invoice not found.', 'danger')
                return redirect(url_for('index'))
    finally:
        conn.close()
    return render_template('invoice.html', inv=inv)


# ─────────────────────────────────────────
# Admin routes
# ─────────────────────────────────────────

@app.route('/admin')
def admin_dashboard():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM courses")
            total_courses = cur.fetchone()['cnt']
            cur.execute("SELECT COUNT(*) AS cnt FROM registrations")
            total_regs = cur.fetchone()['cnt']
            cur.execute("SELECT COUNT(*) AS cnt FROM delegates")
            total_delegates = cur.fetchone()['cnt']
            cur.execute("SELECT COALESCE(SUM(amount),0) AS revenue FROM invoices")
            revenue = cur.fetchone()['revenue']
            cur.execute("""
                SELECT c.course_name, COUNT(r.registration_id) AS enrolled, c.max_delegates
                FROM courses c
                LEFT JOIN registrations r ON c.course_id = r.course_id
                GROUP BY c.course_id
                ORDER BY enrolled DESC
            """)
            course_stats = cur.fetchall()
    finally:
        conn.close()
    return render_template('admin/dashboard.html',
                           total_courses=total_courses,
                           total_regs=total_regs,
                           total_delegates=total_delegates,
                           revenue=revenue,
                           course_stats=course_stats)


@app.route('/admin/registrations')
def admin_registrations():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT r.registration_id, r.registration_date, r.fee_paid,
                       r.registered_by_employee,
                       d.name AS delegate_name, d.email,
                       c.course_name,
                       co.company_name,
                       i.payee_type, i.invoice_id
                FROM registrations r
                JOIN delegates d ON r.delegate_id = d.delegate_id
                JOIN courses c ON r.course_id = c.course_id
                LEFT JOIN companies co ON r.company_id = co.company_id
                LEFT JOIN invoices i ON r.registration_id = i.registration_id
                ORDER BY r.registration_date DESC
            """)
            registrations = cur.fetchall()
    finally:
        conn.close()
    return render_template('admin/registrations.html', registrations=registrations)


@app.route('/admin/courses')
def admin_courses():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.*, cd.delivery_date, v.venue_name,
                       ep.provider_name,
                       COUNT(r.registration_id) AS enrolled
                FROM courses c
                LEFT JOIN course_deliveries cd ON c.course_id = cd.course_id
                LEFT JOIN venues v ON cd.venue_id = v.venue_id
                LEFT JOIN external_providers ep ON c.provider_id = ep.provider_id
                LEFT JOIN registrations r ON c.course_id = r.course_id
                GROUP BY c.course_id, cd.delivery_id
                ORDER BY cd.delivery_date
            """)
            courses = cur.fetchall()
    finally:
        conn.close()
    return render_template('admin/courses.html', courses=courses)


@app.route('/admin/add_course', methods=['GET', 'POST'])
def add_course():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM venues")
            venues = cur.fetchall()
            cur.execute("SELECT * FROM external_providers")
            providers = cur.fetchall()

        if request.method == 'POST':
            name = request.form['course_name'].strip()
            desc = request.form['description'].strip()
            max_d = int(request.form['max_delegates'])
            fee = Decimal(request.form['base_fee'])
            outsourced = 'is_outsourced' in request.form
            provider_id = request.form.get('provider_id') or None
            venue_id = int(request.form['venue_id'])
            delivery_date = request.form['delivery_date']

            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO courses (course_name, description, max_delegates, base_fee, is_outsourced, provider_id)
                    VALUES (%s,%s,%s,%s,%s,%s)
                """, (name, desc, max_d, fee, outsourced, provider_id))
                course_id = cur.lastrowid
                cur.execute("""
                    INSERT INTO course_deliveries (course_id, venue_id, delivery_date)
                    VALUES (%s,%s,%s)
                """, (course_id, venue_id, delivery_date))
            conn.commit()
            flash('Course added successfully.', 'success')
            return redirect(url_for('admin_courses'))
    finally:
        conn.close()

    return render_template('admin/add_course.html', venues=venues, providers=providers)


if __name__ == '__main__':
    app.run(debug=True)
