import collections
from app import app
from models import db, Supplier, SupplierProduct

with app.app_context():
    supplier = Supplier.query.first()
    if supplier:
        print(f"Supplier ID: {supplier.id}, Name: {supplier.name}")
        products = SupplierProduct.query.filter_by(supplier_id=supplier.id).all()
        print(f"Total products for supplier: {len(products)}")
        statuses = collections.Counter(p.status for p in products)
        print(f"Statuses: {statuses}")
        
    else:
        print("No suppliers found.")
