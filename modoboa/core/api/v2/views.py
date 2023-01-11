"""Core API v2 views."""

import logging

from django.utils.html import escape
from django.utils.translation import ugettext as _
from django.utils.datastructures import MultiValueDictKeyError

from django.contrib.auth import login

from rest_framework import response, status
from rest_framework_simplejwt import views as jwt_views
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework.views import APIView

from modoboa.core.password_hashers import get_password_hasher
from modoboa.parameters import tools as param_tools

from smtplib import SMTPException

from . import serializers

logger = logging.getLogger("modoboa.auth")


class TokenObtainPairView(jwt_views.TokenObtainPairView):
    """We overwrite this view to deal with password scheme update."""

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
        except TokenError as e:
            logger.warning(
                _("Failed connection attempt from '%s' as user '%s'"),
                request.META["REMOTE_ADDR"],
                escape(serializer.data["username"])
            )
            raise InvalidToken(e.args[0])

        user = serializer.user
        login(request, user)
        logger.info(
            _("User '%s' successfully logged in"), user.username
        )
        if user and user.is_active:
            condition = (
                user.is_local and
                param_tools.get_global_parameter(
                    "update_scheme", raise_exception=False)
            )
            if condition:
                # check if password scheme is correct
                scheme = param_tools.get_global_parameter(
                    "password_scheme", raise_exception=False)
                # use SHA512CRYPT as default fallback
                if scheme is None:
                    pwhash = get_password_hasher("sha512crypt")()
                else:
                    pwhash = get_password_hasher(scheme)()
                if not user.password.startswith(pwhash.scheme):
                    logger.info(
                        _("Password scheme mismatch. Updating %s password"),
                        user.username
                    )
                    user.set_password(request.data["password"])
                    user.save()
                if pwhash.needs_rehash(user.password):
                    logger.info(
                        _("Password hash parameter missmatch. "
                          "Updating %s password"),
                        user.username
                    )
                    user.set_password(serializer.data["password"])
                    user.save()

        return response.Response(
            serializer.validated_data, status=status.HTTP_200_OK)


class EmailPasswordResetView(APIView):
    """
    An Api View which provides a method to request a password reset token based on an e-mail address.
    """

    def post(self, request, *args, **kwargs):
        serializer = serializers.PasswordRecoveryEmailSerializer(
            data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        try:
            serializer.save()
        except SMTPException:
            return response.Response({
                "type": "email",
                "reason": "Error while sending the email. Please contact an administrator."
            }, 503)
        # Email response
        return response.Response({"type": "email"}, 200)


class DefaultPasswordResetView(EmailPasswordResetView):
    """
    Works with PasswordRecoveryForm.vue.
    First checks if SMS recovery is available, else switch to super (Email recovery [with secondary email]).
    """

    def post(self, request, *args, **kwargs):
        """Recover password."""
        serializer = serializers.PasswordRecoverySmsSerializer(
            data=request.data, context={'request': request})
        try:
            serializer.is_valid(raise_exception=True)
        except serializers.NoSMSAvailable:
            return super().post(request, *args, **kwargs)
        # SMS response
        return response.Response({"type": "sms"}, 200)


class PasswordResetSmsTOTP(APIView):
    """ Check SMS Totp code. """

    def post(self, request, *args, **kwargs):
        try:
            if request.data["type"] == "confirm":
                klass = serializers.PasswordRecoverySmsConfirmSerializer
            elif request.data["type"] == "resend":
                klass = serializers.PasswordRecoverySmsResendSerializer
            serializer = klass(data=request.data, context={'request': request})
        except (MultiValueDictKeyError, KeyError):
            return response.Response({"reason": "No type provided."}, 400)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        payload = {"type": "resend"}
        if request.data["type"] == "confirm":
            serializer_response = serializer.context["response"]
            payload.update({
                "token": serializer_response[0],
                "id": serializer_response[1],
                "type": "confirm"
            })
        return response.Response(payload, 200)


class PasswordResetConfirmView(APIView):
    """ Get and set new user password. """

    def post(self, request, *args, **kwargs):
        serializer = serializers.PasswordRecoveryConfirmSerializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
        except serializers.PasswordRequirementsFailure as e:
            data = {"type": "password_requirement"}
            errors = []
            for element in e.message_list:
                errors.append(element)
            data.update({"errors": errors})
            return response.Response(data, 400)
        serializer.save()
        return response.Response(status=200)
