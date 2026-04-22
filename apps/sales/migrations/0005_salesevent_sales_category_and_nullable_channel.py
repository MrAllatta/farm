# Generated manually for category-level plan rows (workbook 302).

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [
        ("reference", "0005_salescategory_saleschannel_category_salesplanbucket_and_more"),
        ("sales", "0004_salesevent_drawn_from_return"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="salesevent",
            name="sales_event_kind_channel_date_product_uniq",
        ),
        migrations.AddField(
            model_name="salesevent",
            name="sales_category",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="sales_events",
                to="reference.salescategory",
            ),
        ),
        migrations.AlterField(
            model_name="salesevent",
            name="channel",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="reference.saleschannel",
            ),
        ),
        migrations.AddConstraint(
            model_name="salesevent",
            constraint=models.CheckConstraint(
                condition=(
                    ~Q(entry_kind="plan", channel__isnull=True, sales_category__isnull=True)
                    & ~Q(entry_kind="actual", channel__isnull=True)
                ),
                name="salesevent_plan_or_actual_needs_channel_or_cat",
            ),
        ),
        migrations.AddConstraint(
            model_name="salesevent",
            constraint=models.UniqueConstraint(
                condition=Q(entry_kind="plan", channel__isnull=False),
                fields=("entry_kind", "channel", "sale_date", "product"),
                name="salesevent_uniq_plan_channel_date_product",
            ),
        ),
        migrations.AddConstraint(
            model_name="salesevent",
            constraint=models.UniqueConstraint(
                condition=Q(entry_kind="plan", channel__isnull=True, sales_category__isnull=False),
                fields=("entry_kind", "sales_category", "sale_date", "product"),
                name="salesevent_uniq_plan_category_date_product",
            ),
        ),
        migrations.AddConstraint(
            model_name="salesevent",
            constraint=models.UniqueConstraint(
                condition=Q(entry_kind="actual"),
                fields=("entry_kind", "channel", "sale_date", "product"),
                name="salesevent_uniq_actual_channel_date_product",
            ),
        ),
    ]
