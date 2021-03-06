# Generated by Django 2.1.2 on 2019-02-19 02:13

from django.conf import settings
import django.contrib.postgres.fields
import django.contrib.postgres.fields.jsonb
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Area',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('type', models.CharField(max_length=20)),
                ('title', models.CharField(max_length=255)),
                ('description', models.CharField(max_length=2044)),
                ('ccodes', django.contrib.postgres.fields.ArrayField(base_field=models.CharField(max_length=2), blank=True, null=True, size=None)),
                ('geojson', django.contrib.postgres.fields.jsonb.JSONField(blank=True, null=True)),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('owner', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='areas', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'db_table': 'areas',
                'managed': True,
            },
        ),
    ]
