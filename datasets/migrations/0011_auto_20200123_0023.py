# Generated by Django 2.2.4 on 2020-01-23 00:23

import datasets.models
from django.conf import settings
import django.contrib.postgres.fields
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('datasets', '0010_auto_20200111_1723'),
    ]

    operations = [
        migrations.AlterField(
            model_name='datasetuser',
            name='user_id',
            field=models.ForeignKey(default=-1, on_delete=django.db.models.deletion.CASCADE, related_name='users', to=settings.AUTH_USER_MODEL),
        ),
        migrations.CreateModel(
            name='DatasetFile',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('rev', models.IntegerField(blank=True, null=True)),
                ('file', models.FileField(upload_to=datasets.models.user_directory_path)),
                ('uri_base', models.URLField(blank=True, default='http://whgazetteer.org/api/places/', null=True)),
                ('format', models.CharField(choices=[('lpf', 'Linked Places v1.0'), ('delimited', 'LP-TSV')], default='lpf', max_length=12)),
                ('datatype', models.CharField(choices=[('place', 'Places'), ('anno', 'Traces')], default='place', max_length=12)),
                ('delimiter', models.CharField(blank=True, max_length=5, null=True)),
                ('status', models.CharField(blank=True, choices=[('format_error', 'Invalid format'), ('format_ok', 'Valid format'), ('in_database', 'Inserted to database'), ('uploaded', 'File uploaded'), ('ready', 'Ready for submittal'), ('accessioned', 'Accessioned')], max_length=12, null=True)),
                ('upload_date', models.DateTimeField(auto_now_add=True, null=True)),
                ('accepted_date', models.DateTimeField(null=True)),
                ('header', django.contrib.postgres.fields.ArrayField(base_field=models.CharField(max_length=30), blank=True, null=True, size=None)),
                ('numrows', models.IntegerField(blank=True, null=True)),
                ('dataset_id', models.ForeignKey(default=-1, on_delete=django.db.models.deletion.CASCADE, related_name='files', to='datasets.Dataset')),
            ],
            options={
                'db_table': 'dataset_file',
                'managed': True,
            },
        ),
    ]
