from app.models.admin_user import AdminUser
from app.models.brand import Brand
from app.models.category import Category
from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product
from app.models.upload import Upload

all_models = [
    AdminUser,
    Brand,
    Category,
    Product,
    Order,
    OrderItem,
    Upload,
]
