# Generated by Django 2.2.10 on 2020-03-02 16:01

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('places', '0005_auto_20190916_1820'),
    ]

    operations = [
        migrations.AddField(
            model_name='place',
            name='indexed',
            field=models.BooleanField(default=False),
        ),
    ]
