from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import transaction
from djoser.serializers import UserSerializer
from drf_extra_fields.fields import Base64ImageField
from rest_framework import serializers, status
from rest_framework.exceptions import APIException
from rest_framework.generics import get_object_or_404

from recipes.models import (AmountIngredient, Favorite, Ingredient, Recipe,
                            ShoppingCart, Tag)
from users.models import Subscription, User


class UserSerializer(serializers.ModelSerializer):
    is_subscribed = serializers.SerializerMethodField(read_only=True)

    class Meta(UserSerializer.Meta):
        model = get_user_model()
        fields = (
            "email",
            "id",
            "username",
            "first_name",
            "last_name",
            "is_subscribed",
        )

    # @login_required
    def get_is_subscribed(self, obj):
        user = self.context.get("request").user
        if user.is_authenticated:
            return user.followed_users.filter(user=user, author=obj).exists()
        return False


class SubscribeSerializer(UserSerializer):
    recipes_count = serializers.ReadOnlyField(source="recipes.count")
    recipes = serializers.SerializerMethodField()

    class Meta(UserSerializer.Meta):
        fields = UserSerializer.Meta.fields + (
            "recipes",
            "recipes_count",
        )
        read_only_fields = ("email", "username", "first_name", "last_name")

    def get_recipes_count(self, obj):
        return obj.recipes.count()

    def get_recipes(self, obj):
        recipes_limit = self.context["request"].GET.get("recipes_limit")
        if recipes_limit:
            return RecipeShortSerializer(
                Recipe.objects.filter(author=obj)[: int(recipes_limit)],
                many=True,
            ).data
        return RecipeShortSerializer(
            Recipe.objects.filter(author=obj), many=True
        ).data


class SubscribeCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Subscription
        fields = ('user', 'author')

    def validate(self, data):
        try:
            user_id = data.get("user").id
            author_id = data.get("author").id
            get_object_or_404(User, id=author_id)
        except User.DoesNotExist:
            raise APIException(
                detail="Пользователь с таким id не существует!",
                code=status.HTTP_404_NOT_FOUND,
            )
        if Subscription.objects.filter(
                author=author_id, user=user_id).exists():
            raise serializers.ValidationError(
                detail="Вы уже подписаны на этого пользователя!",
                code=status.HTTP_400_BAD_REQUEST,
            )
        if user_id == author_id:
            raise serializers.ValidationError(
                detail="Вы не можете подписаться на самого себя!",
                code=status.HTTP_400_BAD_REQUEST,
            )
        return data

    def to_representation(self, instance):
        request = self.context.get('request')
        return SubscribeSerializer(
            instance.author, context={'request': request}
        ).data


class TagSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = ("id", "name", "color", "slug")


class IngredientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Ingredient
        fields = ("id", "name", "measurement_unit")


class AmountIngredientSerializer(serializers.ModelSerializer):
    id = serializers.ReadOnlyField(source="ingredient.id")
    name = serializers.ReadOnlyField(source="ingredient.name")
    measurement_unit = serializers.ReadOnlyField(
        source="ingredient.measurement_unit"
    )

    class Meta:
        model = AmountIngredient
        fields = ("id", "name", "measurement_unit", "amount")


class CreateAmountIngredientSerializer(serializers.ModelSerializer):
    id = serializers.PrimaryKeyRelatedField(
        queryset=Ingredient.objects.all(), )
    amount = serializers.IntegerField(min_value=settings.MIN_VALUE,
                                      max_value=settings.MAX_VALUE)

    class Meta:
        fields = ("id", "amount")
        model = AmountIngredient


class RecipeReadSerializer(serializers.ModelSerializer):
    image = Base64ImageField()
    author = UserSerializer(read_only=True)
    tags = TagSerializer(many=True, read_only=True)
    ingredients = AmountIngredientSerializer(
        many=True,
        source="recipe_ingredient",
    )
    is_favorited = serializers.SerializerMethodField()
    is_in_shopping_cart = serializers.SerializerMethodField()

    class Meta:
        model = Recipe
        fields = (
            "id",
            "tags",
            "author",
            "ingredients",
            "is_favorited",
            "is_in_shopping_cart",
            "name",
            "image",
            "text",
            "cooking_time",
        )

    def get_is_favorited(self, obj):
        return self.get_is_in_user_field(obj, "recipes_favorite_related")

    def get_is_in_shopping_cart(self, obj):
        return self.get_is_in_user_field(obj, "recipes_shoppingcart_related")

    def get_is_in_user_field(self, obj, field):
        user = self.context.get("request").user
        if user.is_authenticated:
            return getattr(user, field).filter(recipe=obj).exists()
        return False


class RecipeCreateSerializer(serializers.ModelSerializer):
    image = Base64ImageField()
    author = UserSerializer(read_only=True)
    tags = serializers.PrimaryKeyRelatedField(
        queryset=Tag.objects.all(), many=True
    )
    ingredients = CreateAmountIngredientSerializer(many=True, write_only=True)
    cooking_time = serializers.IntegerField(
        validators=(MinValueValidator(
            settings.MIN_VALUE,
            message=(f'Время приготовления не может быть меньше'
                     f' {settings.MIN_VALUE} минуты.')
        ), MaxValueValidator(
            settings.MAX_VALUE,
            message=(f'Время приготовления не может быть'
                     f' больше {settings.MAX_VALUE} минут.')))
    )

    class Meta:
        model = Recipe
        fields = (
            "id",
            "tags",
            "author",
            "ingredients",
            "name",
            "image",
            "text",
            "cooking_time",
        )

    def validate(self, data):
        ingredients = data.get("ingredients")
        if not ingredients:
            raise serializers.ValidationError(
                {"ingredients": "Поле ингредиентов не может быть пустым!"}
            )
        ingredients_id = [item['id'] for item in ingredients]
        for ingredient in ingredients_id:
            if ingredients_id.count(ingredient) > 1:
                raise serializers.ValidationError(
                    'Ингридиенты не должны повторяться!'
                )
        ingredient_amount = [item['amount'] for item in ingredients]
        for amount in ingredient_amount:
            if int(amount) < settings.MIN_VALUE:
                raise serializers.ValidationError(
                    'Ошибка! Кол-во ингредиента указано меньше 1'
                )
        tags = data.get("tags")
        if not tags:
            raise serializers.ValidationError(
                {"tags": "Поле тегов не может быть пустым!"}
            )
        if len({tag.id for tag in tags}) != len(tags):
            raise serializers.ValidationError(
                {"tags": "Теги не должны повторяться!"}
            )
        return data

    def validate_image(self, image):
        if not self.instance and not image:
            raise serializers.ValidationError(
                {"image": "Поле изображения не может быть пустым!"}
            )
        if self.instance and not self.instance.images:
            raise serializers.ValidationError(
                {"image": "Поле изображения не может быть пустым!"}
            )
        return image

    @staticmethod
    def create_ingredients(recipe, ingredients):
        create_ingredients = [
            AmountIngredient(
                recipe=recipe, ingredient=ingredient['id'],
                amount=ingredient['amount']
            )
            for ingredient in ingredients
        ]
        AmountIngredient.objects.bulk_create(create_ingredients)

    @transaction.atomic
    def create(self, validated_data):
        current_user = self.context["request"].user
        tags = validated_data.pop("tags")
        ingredients = validated_data.pop("ingredients")
        recipe = Recipe.objects.create(**validated_data, author=current_user)
        recipe.tags.set(tags)
        self.create_ingredients(recipe, ingredients)
        return recipe

    @transaction.atomic
    def update(self, instance, validated_data):
        instance.tags.clear()
        instance.tags.set(validated_data.pop("tags"))
        instance.ingredients.clear()
        ingredients = validated_data.pop("ingredients")
        self.create_ingredients(instance, ingredients)
        return super().update(instance, validated_data)

    def to_representation(self, recipe):
        return RecipeReadSerializer(recipe, context=self.context).data


class RecipeShortSerializer(serializers.ModelSerializer):
    image = Base64ImageField()

    class Meta:
        model = Recipe
        fields = ("id", "name", "image", "cooking_time")


class ShoppingCartCreateDeleteSerializer(serializers.ModelSerializer):
    class Meta:
        model = ShoppingCart
        fields = ("user", "recipe")

    def validate(self, data):
        user_id = data.get("user").id
        recipe_id = data.get("recipe").id
        if not Recipe.objects.filter(id=recipe_id).exists():
            raise serializers.ValidationError("Рецепт не найден")
        if self.Meta.model.objects.filter(user=user_id,
                                          recipe=recipe_id).exists():
            raise serializers.ValidationError("Рецепт уже добавлен")
        return data

    def to_representation(self, instance):
        serializer = RecipeShortSerializer(
            instance.recipe, context=self.context
        )
        return serializer.data


class FavoriteCreateDeleteSerializer(ShoppingCartCreateDeleteSerializer):
    class Meta(ShoppingCartCreateDeleteSerializer.Meta):
        model = Favorite
