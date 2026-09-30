"""Legacy zero-nmID cleanup must preserve history and seller boundaries."""

import pytest
from flask import Flask

from models import CardEditHistory, Product, Seller, User, db
from services.wb_catalog_lifecycle import deactivate_unconfirmed_products


@pytest.fixture
def catalog():
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
    db.init_app(app)
    with app.app_context():
        db.create_all()
        sellers = [
            Seller(company_name=f'Synthetic {index}', user=User(username=f'seller-{index}', email=f'{index}@example.test', password_hash='synthetic'))
            for index in (1, 2)
        ]
        db.session.add_all(sellers)
        db.session.flush()
        products = [
            Product(seller_id=sellers[0].id, nm_id=0, is_active=True),
            Product(seller_id=sellers[0].id, nm_id=101, is_active=True),
            Product(seller_id=sellers[1].id, nm_id=0, is_active=True),
        ]
        db.session.add_all(products)
        db.session.flush()
        history = CardEditHistory(
            product_id=products[0].id, seller_id=sellers[0].id,
            action='create', snapshot_after={'title': 'Preserved source'},
        )
        db.session.add(history)
        db.session.commit()
        yield sellers, products, history
        db.session.remove()
        db.drop_all()


def test_deactivation_preserves_product_history_and_other_seller(catalog):
    sellers, products, history = catalog
    product_id, history_id = products[0].id, history.id
    assert deactivate_unconfirmed_products(sellers[0].id) == 1
    db.session.commit()
    assert db.session.get(Product, product_id).is_active is False
    assert db.session.get(CardEditHistory, history_id).product_id == product_id
    assert db.session.get(CardEditHistory, history_id).snapshot_after == {'title': 'Preserved source'}
    assert db.session.get(Product, products[1].id).is_active is True
    assert db.session.get(Product, products[2].id).is_active is True
    assert deactivate_unconfirmed_products(sellers[0].id) == 0


def test_deactivation_is_rollbackable_and_bounded(catalog):
    sellers, products, _ = catalog
    db.session.add(Product(seller_id=sellers[0].id, nm_id=-1, is_active=True))
    db.session.commit()
    assert deactivate_unconfirmed_products(sellers[0].id, limit=1) == 1
    db.session.rollback()
    assert db.session.get(Product, products[0].id).is_active is True
    assert deactivate_unconfirmed_products(sellers[0].id, limit=1) == 1
    db.session.commit()
    assert deactivate_unconfirmed_products(sellers[0].id, limit=1) == 1


@pytest.mark.parametrize('seller_id', [None, True, 0, -1, '1'])
def test_deactivation_rejects_untyped_seller(catalog, seller_id):
    with pytest.raises(ValueError):
        deactivate_unconfirmed_products(seller_id)
